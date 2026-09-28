"""Plex integration: scans, matching, metadata/poster push, removals.

Everything runs in one background worker at a steady rate
(plex_rate_per_second), so importing a channel with tens of thousands of
videos never floods Plex. Work is derived from state in the database (no
separate queue that could be lost on restart):

  scan       subscription folders that changed -> partial scan
             (/library/sections/{id}/refresh?path=<prefix>/<show folder>)
  match      entries without a Plex ratingKey or not yet analyzed -> read the
             section's recently added episodes, map part files to entries
  metadata   matched entries without meta_pushed_at -> PUT title, cleaned
             summary, originallyAvailableAt, all locked
  posters    matched entries without poster_pushed_at -> POST .../posters?url=
             (i.ytimg.com maxresdefault, fallback hqdefault), locked
  shows      subscriptions whose show item isn't pushed -> title, summary
             (channel/playlist description), poster (avatar), art (banner)
  delete     removed entries still in Plex -> DELETE /library/metadata/{key}
  analyze    after a download finishes -> PUT .../analyze, so Plex replaces
             the stub's guessed resolution/bitrate with the real ones
"""

import logging
import threading
import time
from collections import deque
from urllib.parse import quote

import requests

from . import describe, layout

log = logging.getLogger("youtubarr.plex")

MATCH_INTERVAL_PENDING = 20
MATCH_INTERVAL_IDLE = 600
DELETE_RETRY_SECS = 300


class PlexError(Exception):
    pass


class Plex:
    def __init__(self, db):
        self.db = db
        self.session = requests.Session()

    def configured(self):
        return bool(self.url() and self.section())

    def url(self):
        return (self.db.setting("plex_url") or "").strip().rstrip("/")

    def section(self):
        return (self.db.setting("plex_section_id") or "").strip()

    def _req(self, method, path, params=None, timeout=60):
        headers = {"Accept": "application/json", "X-Plex-Client-Identifier": "youtubarr",
                   "X-Plex-Product": "Youtubarr"}
        token = (self.db.setting("plex_token") or "").strip()
        if token:
            headers["X-Plex-Token"] = token
        for attempt in (1, 2):
            try:
                resp = self.session.request(method, self.url() + path, params=params, headers=headers, timeout=timeout)
                break
            except requests.ConnectionError as e:
                # Plex drops idle keep-alive connections; the pooled one we
                # reuse may already be closed. One immediate retry.
                if attempt == 2:
                    raise PlexError(f"{method} {path}: {e}") from e
            except requests.RequestException as e:
                raise PlexError(f"{method} {path}: {e}") from e
        if resp.status_code >= 400:
            raise PlexError(f"{method} {path}: HTTP {resp.status_code}")
        if "json" in resp.headers.get("Content-Type", "") and resp.content:
            return resp.json()
        return None

    def test(self):
        ident = self._req("GET", "/identity")["MediaContainer"]
        sections = self._req("GET", "/library/sections")["MediaContainer"].get("Directory", []) or []
        return {
            "version": ident.get("version"),
            "sections": [{"key": d["key"], "title": d["title"], "type": d["type"], "agent": d.get("agent")}
                         for d in sections],
        }

    def scan(self, folder=None):
        """Scan one show folder, or the whole section when folder is None.
        Plex ignores a partial scan of a folder it has never seen (tested on
        1.43: nothing happens, even with force=1), so a show that isn't in
        Plex yet needs a full scan once; after that partial scans work."""
        if folder is None:
            self._req("GET", f"/library/sections/{self.section()}/refresh")
            return
        prefix = (self.db.setting("plex_path_prefix") or "").rstrip("/")
        path = f"{prefix}/{folder}" if prefix else folder
        # Encoded by hand: requests would send spaces as "+", which Plex reads
        # literally, so the folder doesn't exist for it and nothing is scanned.
        self._req("GET", f"/library/sections/{self.section()}/refresh?path={quote(path, safe='/')}")

    def episodes(self, added_since=None):
        """All episodes of the section (optionally only those Plex added after
        a timestamp), paged."""
        out, start = [], 0
        while True:
            params = {"type": 4, "X-Plex-Container-Start": start, "X-Plex-Container-Size": 1000}
            if added_since:
                params["addedAt>>="] = int(added_since)
            data = self._req("GET", f"/library/sections/{self.section()}/all", params=params, timeout=120)
            items = (data or {}).get("MediaContainer", {}).get("Metadata", []) or []
            out.extend(items)
            if len(items) < 1000:
                return out
            start += 1000

    def edit(self, plex_type, key, fields):
        """PUT field values and lock them (so Plex never overwrites them)."""
        params = {"type": plex_type, "id": key}
        for name, value in fields.items():
            params[f"{name}.value"] = value
            params[f"{name}.locked"] = 1
        self._req("PUT", f"/library/sections/{self.section()}/all", params=params)

    def lock(self, plex_type, key, *fields):
        params = {"type": plex_type, "id": key, **{f"{f}.locked": 1 for f in fields}}
        self._req("PUT", f"/library/sections/{self.section()}/all", params=params)

    def set_image(self, key, kind, url):
        """kind: 'posters' or 'arts'. Plex fetches the URL itself; nothing is
        stored on our side."""
        self._req("POST", f"/library/metadata/{key}/{kind}", params={"url": url})

    def analyze(self, key):
        self._req("PUT", f"/library/metadata/{key}/analyze")

    def delete(self, key):
        self._req("DELETE", f"/library/metadata/{key}")


class RateLimiter:
    def __init__(self, db):
        self.db = db
        self._next = 0.0

    def wait(self, stop):
        rate = max(0.1, self.db.float_setting("plex_rate_per_second", 2))
        delay = self._next - time.time()
        if delay > 0:
            stop.wait(delay)
        self._next = max(time.time(), self._next) + 1.0 / rate


class PlexWorker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True, name="plex-worker")
        self.app = app
        self.db = app.db
        self.plex = Plex(app.db)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.limiter = RateLimiter(app.db)
        self._scan_due = {}           # subscription id -> earliest scan time
        self._analyze = deque()       # plex keys
        self._delete_keys = deque()   # plex keys of items without an entry
        self._lock = threading.Lock()
        self._last_match = 0.0
        self._delete_failed = {}      # plex key -> retry after
        self.last_error = None
        self.stats = {"scans": 0, "matched": 0, "metadata": 0, "posters": 0, "shows": 0,
                      "deleted": 0, "analyzed": 0}

    # --- called from other threads ------------------------------------------

    def request_scan(self, sub_id, delay=5):
        with self._lock:
            due = time.time() + delay
            self._scan_due[sub_id] = min(self._scan_due.get(sub_id, due), due)
        self.wake.set()

    def request_analyze(self, video_id):
        for row in self.db.q("SELECT plex_key FROM entries WHERE video_id = ? AND plex_key IS NOT NULL", (video_id,)):
            with self._lock:
                self._analyze.append(row["plex_key"])
        self.wake.set()

    def request_delete_key(self, key):
        """Delete a Plex item no entry points at any more (renamed file)."""
        with self._lock:
            self._delete_keys.append(key)
        self.wake.set()

    def queue_sizes(self):
        one = lambda sql: self.db.one(sql)["n"]  # noqa: E731
        return {
            "unmatched": one("SELECT COUNT(*) n FROM entries WHERE removed = 0 AND plex_key IS NULL"),
            "metadata": one("SELECT COUNT(*) n FROM entries WHERE removed = 0 AND plex_key IS NOT NULL AND meta_pushed_at IS NULL"),
            "posters": one("SELECT COUNT(*) n FROM entries WHERE removed = 0 AND plex_key IS NOT NULL AND poster_pushed_at IS NULL"),
            "deletes": one("SELECT COUNT(*) n FROM entries WHERE removed = 1 AND plex_key IS NOT NULL AND plex_deleted_at IS NULL"),
            "scans": len(self._scan_due),
            "analyze": len(self._analyze),
            "loose_deletes": len(self._delete_keys),
        }

    # --- loop -------------------------------------------------------------------

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                if self.plex.configured():
                    busy = self.step()
                    self.last_error = None
                else:
                    busy = False
            except PlexError as e:
                self.last_error = str(e)
                log.warning(f"plex: {e}")
                busy = False
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("plex worker")
                busy = False
            if not busy:
                self.wake.wait(10)

    def step(self):
        """One round of work; True if there's more to do right away."""
        now = time.time()
        self._do_scans(now)
        self._do_match(now)
        did = self._do_analyze()
        did = self._do_deletes(now) or did
        did = self._do_shows() or did
        did = self._do_metadata() or did
        did = self._do_posters() or did
        self._finish_deleted_subscriptions()
        return did

    def _do_scans(self, now):
        with self._lock:
            due = [sid for sid, t in self._scan_due.items() if t <= now]
            for sid in due:
                self._scan_due.pop(sid, None)
        full_done = False
        for sid in due:
            sub = self.db.subscription(sid)
            if not sub:
                continue
            new_show = not sub["plex_show_key"] and sub["enabled"] >= 0
            if new_show and full_done:
                continue  # one full scan covers every new show in this round
            self.limiter.wait(self.stop)
            self.plex.scan(None if new_show else sub["folder"])
            full_done = full_done or new_show
            self.stats["scans"] += 1
            log.info(f"plex: {'full scan for new show' if new_show else 'scanning'} {sub['folder']}")
            self._last_match = 0  # look for the new items soon

    def _do_match(self, now):
        pending = self.db.one(
            """SELECT COUNT(*) n, MIN(added_at) since FROM entries
               WHERE removed = 0 AND (plex_key IS NULL OR plex_analyzed_at IS NULL)"""
        )
        interval = MATCH_INTERVAL_PENDING if pending["n"] else MATCH_INTERVAL_IDLE
        if now - self._last_match < interval:
            return
        self._last_match = now
        # Plex's addedAt is its own scan time, always after our added_at.
        since = (pending["since"] - 3600) if pending["n"] else now - 2 * MATCH_INTERVAL_IDLE
        lookup = {}
        for folder, seasons in self.app.tree().items():
            for season, files in seasons.items():
                for name, key in files.items():
                    lookup[(folder, season, name)] = key[:2]
        shows = {}
        for item in self.plex.episodes(added_since=since):
            for media in item.get("Media") or []:
                for part in media.get("Part") or []:
                    path = part.get("file", "").replace("\\", "/").split("/")
                    if len(path) < 3:
                        continue
                    key = lookup.get((path[-3], path[-2], path[-1]))
                    if not key:
                        continue
                    sub_id, video_id = key
                    entry = self.db.entry(sub_id, video_id)
                    if not entry:
                        continue
                    fields = {}
                    if entry["plex_key"] != item.get("ratingKey"):
                        fields.update(plex_key=item.get("ratingKey"), meta_pushed_at=None, poster_pushed_at=None)
                    if media.get("videoCodec") and not entry["plex_analyzed_at"]:
                        fields["plex_analyzed_at"] = time.time()
                    if fields:
                        self.db.update_entry(sub_id, video_id, **fields)
                        self.stats["matched"] += 1
                    if item.get("grandparentRatingKey"):
                        shows[sub_id] = item["grandparentRatingKey"]
        for sub_id, show_key in shows.items():
            sub = self.db.subscription(sub_id)
            if sub and sub["plex_show_key"] != show_key:
                self.db.update_subscription(sub_id, plex_show_key=show_key, show_pushed_at=None)

    def _do_analyze(self):
        with self._lock:
            key = self._analyze.popleft() if self._analyze else None
        if not key:
            return False
        self.limiter.wait(self.stop)
        self.plex.analyze(key)
        self.stats["analyzed"] += 1
        return True

    def _do_deletes(self, now):
        with self._lock:
            loose = list(self._delete_keys)
            self._delete_keys.clear()
        for key in loose:
            self.limiter.wait(self.stop)
            try:
                self.plex.delete(key)
                self.stats["deleted"] += 1
            except PlexError as e:
                log.warning(f"plex: could not delete item {key}: {e}")
        rows = self.db.q(
            """SELECT subscription_id, video_id, plex_key FROM entries
               WHERE removed = 1 AND plex_key IS NOT NULL AND plex_deleted_at IS NULL LIMIT 20"""
        )
        did = False
        for r in rows:
            if self._delete_failed.get(r["plex_key"], 0) > now:
                continue
            self.limiter.wait(self.stop)
            try:
                self.plex.delete(r["plex_key"])
            except PlexError as e:
                if "HTTP 404" not in str(e):
                    # Usually "Allow media deletion" is off in Plex.
                    log.warning(f"plex: could not delete {r['video_id']} ({e}); retrying later")
                    self._delete_failed[r["plex_key"]] = now + DELETE_RETRY_SECS
                    continue
            self.db.update_entry(r["subscription_id"], r["video_id"], plex_deleted_at=time.time())
            self.stats["deleted"] += 1
            did = True
        return did

    def _finish_deleted_subscriptions(self):
        for sub in self.db.q("SELECT * FROM subscriptions WHERE enabled = -1"):
            left = self.db.one(
                """SELECT COUNT(*) n FROM entries WHERE subscription_id = ?
                   AND plex_key IS NOT NULL AND plex_deleted_at IS NULL""", (sub["id"],))["n"]
            if left:
                continue
            if sub["plex_show_key"]:
                try:
                    self.plex.delete(sub["plex_show_key"])
                except PlexError as e:
                    if "HTTP 404" not in str(e):
                        continue
            self.db.delete_subscription(sub["id"])
            self.app.invalidate()
            log.info(f"subscription {sub['title']} removed")

    def _rules_for(self, sub):
        import json
        try:
            overrides = json.loads(sub.get("rules") or "{}")
        except ValueError:
            overrides = {}
        return describe.effective_rules(self.db.rules(), overrides)

    def _do_shows(self):
        sub = self.db.one(
            """SELECT * FROM subscriptions WHERE enabled >= 0 AND plex_show_key IS NOT NULL
               AND show_pushed_at IS NULL LIMIT 1""")
        if not sub:
            return False
        key = sub["plex_show_key"]
        self.limiter.wait(self.stop)
        self.plex.edit(2, key, {"title": sub["title"], "summary": describe.clean(sub["description"], self._rules_for(sub))})
        if sub["avatar_url"]:
            self.limiter.wait(self.stop)
            self.plex.set_image(key, "posters", sub["avatar_url"])
        if sub["banner_url"]:
            self.limiter.wait(self.stop)
            self.plex.set_image(key, "arts", sub["banner_url"])
        self.limiter.wait(self.stop)
        self.plex.lock(2, key, "thumb", "art")
        self.db.update_subscription(sub["id"], show_pushed_at=time.time())
        self.stats["shows"] += 1
        return True

    def _do_metadata(self):
        rows = self.db.q(
            """SELECT e.subscription_id, e.video_id, e.plex_key FROM entries e
               WHERE e.removed = 0 AND e.plex_key IS NOT NULL AND e.meta_pushed_at IS NULL LIMIT 20""")
        subs = {}
        for r in rows:
            if self.stop.is_set():
                break
            video = self.db.video(r["video_id"])
            sub = subs.get(r["subscription_id"]) or self.db.subscription(r["subscription_id"])
            subs[r["subscription_id"]] = sub
            if not video or not sub:
                continue
            self.limiter.wait(self.stop)
            self.plex.edit(4, r["plex_key"], {
                "title": video["title"],
                "summary": describe.clean(video["description"], self._rules_for(sub)),
                "originallyAvailableAt": layout.published(video).strftime("%Y-%m-%d"),
            })
            self.db.update_entry(r["subscription_id"], r["video_id"], meta_pushed_at=time.time())
            self.stats["metadata"] += 1
        return bool(rows)

    def _do_posters(self):
        rows = self.db.q(
            """SELECT subscription_id, video_id, plex_key FROM entries
               WHERE removed = 0 AND plex_key IS NOT NULL AND meta_pushed_at IS NOT NULL
               AND poster_pushed_at IS NULL LIMIT 20""")
        for r in rows:
            if self.stop.is_set():
                break
            video = self.db.video(r["video_id"]) or {}
            thumbs = video.get("thumbnails", {})
            # The API only lists "maxres" when that image exists.
            url = thumbs.get("maxres") or thumbs.get("high") or f"https://i.ytimg.com/vi/{r['video_id']}/hqdefault.jpg"
            self.limiter.wait(self.stop)
            self.plex.set_image(r["plex_key"], "posters", url)
            self.limiter.wait(self.stop)
            self.plex.lock(4, r["plex_key"], "thumb")
            self.db.update_entry(r["subscription_id"], r["video_id"], poster_pushed_at=time.time())
            self.stats["posters"] += 1
        return bool(rows)
