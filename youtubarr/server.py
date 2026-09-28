"""HTTP server: virtual library for rclone (/library/...), web UI and API.

Probe vs playback -- the heuristic
----------------------------------
Every read reaches us through rclone as a plain HTTP range request; rclone
hides which Plex process is reading and chunks reads on its own. So the
decision whether to answer with the stub or with real bytes is made per file
(subscription entry), BEFORE the first byte of a request is sent (a stub
header and a real header can't be mixed within one stream):

1. Cached (download finished)       -> real file.
2. Download running                 -> real bytes (the growing file).
3. Plex has NOT analyzed this file  -> stub. The only reader of a brand-new
   file is Plex's own scanner/analyzer; nobody can press play on an item that
   isn't in the library yet. "Analyzed" comes from the Plex API: the Plex
   worker marks an entry once Plex lists its episode with codec info.
4. Plex HAS analyzed it             -> this is a play: start the download.

Plex only re-reads an unchanged file when told to (manual Analyze, or the
scheduled media-analysis / thumbnail tasks, which must be off -- see README).

Fallback while in (3): a probe reads a few KiB of header, then seeks (a new
request) past the Void. A single request that streams more than
probe_fallback_bytes of stub padding sequentially is a player, not a probe
(someone pressed play before the worker saw the analysis). We start the
download and cut that response short; the player's retry gets the real file.
That one attempt fails -- the price of never touching YouTube for a probe.
"""

import base64
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

from . import describe, layout, stub
from .cache import CHUNK, DownloadError
from .db import DEFAULT_RULES, DEFAULT_SETTINGS, SECRET_SETTINGS
from .youtube import YouTubeError, pacific_day

log = logging.getLogger("youtubarr.server")

LIBRARY = "/library/"
UI_PATH = os.path.join(os.path.dirname(__file__), "ui.html")


class RingLog(logging.Handler):
    def __init__(self, size=500):
        super().__init__()
        self.lines = deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass


class App:
    def __init__(self, db, cache, stubs):
        self.db = db
        self.cache = cache
        self.stubs = stubs
        self.plex = None   # PlexWorker, set in __main__
        self.sync = None   # Syncer
        self.ring = None
        self._tree = None
        self._tree_lock = threading.Lock()

    def tree(self):
        with self._tree_lock:
            if self._tree is None:
                self._tree = layout.build(self.db)
            return self._tree

    def invalidate(self):
        with self._tree_lock:
            self._tree = None

    def estimated_size(self, video):
        kbps = self.db.int_setting("estimated_bitrate_kbps", 8000)
        return int(video["duration"] * kbps * 1000 / 8) + 4 * 1024 * 1024

    def size(self, video):
        """What HEAD reports: constant until the download finished (Plex
        re-analyzes a file whose size changed -- by then it's cached)."""
        real = self.cache.cached_size(video["id"])
        return real if real is not None else self.estimated_size(video)

    def mode(self, entry, video):
        if self.cache.cached_size(video["id"]) is not None:
            return "cached"
        if self.cache.job(video["id"]) is not None:
            return "download"
        if entry.get("plex_analyzed_at") is None:
            return "stub"
        return "play"

    def stub_header(self, video):
        return self.stubs.header(video["duration"], self.estimated_size(video),
                                 video["width"], video["height"], title=video["title"])


class Handler(BaseHTTPRequestHandler):
    app = None  # set by serve()
    protocol_version = "HTTP/1.1"
    server_version = "youtubarr"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # --- helpers --------------------------------------------------------------------

    def _send(self, status, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data, default=str).encode(), "application/json")

    def _error(self, message, status=400):
        self._json({"ok": False, "error": message}, status)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def _authorized(self):
        password = self.app.db.setting("ui_password") or ""
        if not password:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _user, _, given = base64.b64decode(auth[6:]).decode().partition(":")
            except Exception:
                return False
            return hmac.compare_digest(given.encode(), password.encode())
        return False

    def _require_auth(self):
        if self._authorized():
            return True
        self._send(401, b"login required", headers={"WWW-Authenticate": 'Basic realm="youtubarr"'})
        return False

    # --- routing ----------------------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path.startswith(LIBRARY) or path == LIBRARY.rstrip("/"):
            return self._library(path[len(LIBRARY):] if path.startswith(LIBRARY) else "")
        if path == "/health":
            return self._json({"ok": True})
        if not self._require_auth():
            return
        if path in ("/", "/ui"):
            with open(UI_PATH, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        routes = {
            "/api/settings": self.api_settings,
            "/api/status": self.api_status,
            "/api/subscriptions": self.api_subscriptions,
        }
        if path in routes:
            return routes[path]()
        m = re.fullmatch(r"/api/subscriptions/(\d+)/videos", path)
        if m:
            return self.api_sub_videos(int(m.group(1)))
        if path == "/api/plex/test":
            return self.api_plex_test()
        return self._send(404, b"not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        body = self._body()
        if path == "/api/settings":
            return self.api_save_settings(body)
        if path == "/api/subscriptions":
            return self.api_add(body)
        if path == "/api/rules/preview":
            rules = describe.effective_rules(self.app.db.rules(), body.get("rules") or {})
            return self._json({"cleaned": describe.clean(body.get("text", ""), rules)})
        m = re.fullmatch(r"/api/subscriptions/(\d+)/(check|repush)", path)
        if m:
            return self.api_sub_action(int(m.group(1)), m.group(2))
        return self._send(404, b"not found")

    def do_PATCH(self):
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        m = re.fullmatch(r"/api/subscriptions/(\d+)", path)
        if m:
            return self.api_sub_update(int(m.group(1)), self._body())
        return self._send(404, b"not found")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        m = re.fullmatch(r"/api/subscriptions/(\d+)", path)
        if m:
            return self.api_sub_delete(int(m.group(1)))
        return self._send(404, b"not found")

    # --- API -----------------------------------------------------------------------------

    def api_settings(self):
        s = self.app.db.settings()
        for secret in SECRET_SETTINGS:
            s[secret + "_set"] = bool(s.get(secret))
            s[secret] = ""
        s["rules"] = self.app.db.rules()
        s["default_rules"] = DEFAULT_RULES
        return self._json(s)

    def api_save_settings(self, body):
        values = {}
        for key, value in body.items():
            if key not in DEFAULT_SETTINGS:
                continue
            if key in SECRET_SETTINGS and value in ("", None):
                continue  # empty = unchanged; the UI never receives secrets
            values[key] = value
        if "rules" in values:
            rules = values["rules"] if isinstance(values["rules"], dict) else json.loads(values["rules"])
            values["rules"] = json.dumps({k: rules[k] for k in DEFAULT_RULES if k in rules})
            # Re-clean everything with the new rules (no YouTube calls).
            self.app.db.x("UPDATE entries SET meta_pushed_at = NULL WHERE plex_key IS NOT NULL")
            self.app.db.x("UPDATE subscriptions SET show_pushed_at = NULL")
        self.app.db.update_settings(values)
        return self._json({"ok": True})

    def api_status(self):
        db = self.app.db
        used, files = self.app.cache.usage()
        counts = db.one("""SELECT
            (SELECT COUNT(*) FROM subscriptions WHERE enabled >= 0) subscriptions,
            (SELECT COUNT(*) FROM entries WHERE removed = 0) entries,
            (SELECT COUNT(*) FROM entries WHERE removed = 0 AND meta_pushed_at IS NOT NULL) in_plex""")
        return self._json({
            "quota_today": db.quota(pacific_day()),
            "cache": {"bytes": used, "files": files, "max_gb": db.int_setting("max_cache_gb", 50),
                      "downloads": self.app.cache.downloads()},
            "counts": counts,
            "plex": {"configured": self.app.plex.plex.configured(), "last_error": self.app.plex.last_error,
                     "queue": self.app.plex.queue_sizes(), "stats": self.app.plex.stats},
            "sync": {"current": self.app.sync.current},
            "log": list(self.app.ring.lines)[-150:] if self.app.ring else [],
        })

    def api_subscriptions(self):
        subs = self.app.db.q(
            """SELECT s.*, (SELECT COUNT(*) FROM entries e WHERE e.subscription_id = s.id AND e.removed = 0) videos,
                      (SELECT COUNT(*) FROM entries e WHERE e.subscription_id = s.id AND e.removed = 0
                              AND e.meta_pushed_at IS NOT NULL) in_plex
               FROM subscriptions s WHERE s.enabled >= 0 ORDER BY s.title COLLATE NOCASE""")
        for s in subs:
            s["rules"] = json.loads(s["rules"] or "{}")
        return self._json(subs)

    def api_sub_videos(self, sub_id):
        rows = self.app.db.q(
            """SELECT e.*, v.title, v.duration, v.published_at FROM entries e JOIN videos v ON v.id = e.video_id
               WHERE e.subscription_id = ? AND e.removed = 0 ORDER BY e.season DESC, e.episode DESC LIMIT 500""",
            (sub_id,))
        for r in rows:
            r["cached"] = self.app.cache.cached_size(r["video_id"]) is not None
        return self._json(rows)

    def api_add(self, body):
        try:
            sub = self.app.sync.add(body.get("input", ""), mode=body.get("mode"),
                                    min_duration=body.get("min_duration") or 0)
        except YouTubeError as e:
            return self._error(str(e))
        return self._json({"ok": True, "subscription": sub})

    def api_sub_update(self, sub_id, body):
        sub = self.app.db.subscription(sub_id)
        if not sub:
            return self._error("not found", 404)
        fields = {}
        if "enabled" in body:
            fields["enabled"] = 1 if body["enabled"] else 0
        if "min_duration" in body:
            fields["min_duration"] = int(body["min_duration"] or 0)
        if "rules" in body:
            rules = {k: v for k, v in (body["rules"] or {}).items() if k in DEFAULT_RULES and v is not None}
            fields["rules"] = json.dumps(rules)
            fields["show_pushed_at"] = None
            self.app.db.x("UPDATE entries SET meta_pushed_at = NULL WHERE subscription_id = ? AND plex_key IS NOT NULL",
                          (sub_id,))
        self.app.db.update_subscription(sub_id, **fields)
        return self._json({"ok": True})

    def api_sub_action(self, sub_id, action):
        if not self.app.db.subscription(sub_id):
            return self._error("not found", 404)
        if action == "check":
            self.app.sync.force(sub_id)
        else:  # repush: metadata + posters again, from what's stored (no API calls)
            self.app.db.x("""UPDATE entries SET meta_pushed_at = NULL, poster_pushed_at = NULL
                             WHERE subscription_id = ?""", (sub_id,))
            self.app.db.update_subscription(sub_id, show_pushed_at=None)
            self.app.plex.wake.set()
        return self._json({"ok": True})

    def api_sub_delete(self, sub_id):
        sub = self.app.db.subscription(sub_id)
        if not sub:
            return self._error("not found", 404)
        # enabled = -1: gone from the listing now; the Plex worker deletes its
        # items and then the subscription row itself.
        self.app.db.update_subscription(sub_id, enabled=-1)
        self.app.db.x("UPDATE entries SET removed = 1 WHERE subscription_id = ?", (sub_id,))
        self.app.invalidate()
        self.app.plex.request_scan(sub_id)
        for row in self.app.db.q("SELECT video_id FROM entries WHERE subscription_id = ?", (sub_id,)):
            if not self.app.db.one("SELECT COUNT(*) n FROM entries WHERE video_id = ? AND removed = 0",
                                   (row["video_id"],))["n"]:
                self.app.cache.remove(row["video_id"])
        return self._json({"ok": True})

    def api_plex_test(self):
        try:
            return self._json({"ok": True, **self.app.plex.plex.test()})
        except Exception as e:
            return self._error(str(e))

    # --- virtual library ---------------------------------------------------------------

    def _listing(self, entries):
        lines = ["<html><body>"]
        for name, is_dir, mtime in entries:
            href = quote(name) + ("/" if is_dir else "")
            stamp = datetime.fromtimestamp(mtime, timezone.utc).strftime("%d-%b-%Y %H:%M")
            lines.append(f'<a href="{href}">{name}{"/" if is_dir else ""}</a> {stamp}')
        lines.append("</body></html>")
        self._send(200, "\n".join(lines).encode(), "text/html; charset=utf-8")

    def _library(self, rel):
        tree = self.app.tree()
        live = {s["folder"] for s in self.app.db.q("SELECT folder FROM subscriptions WHERE enabled >= 0")}
        parts = [p for p in rel.split("/") if p]
        if not parts:
            return self._listing([(f, True, layout.latest(tree[f]) or time.time()) for f in sorted(tree) if f in live])
        seasons = tree.get(parts[0]) if parts[0] in live else None
        if seasons is None:
            return self._send(404, b"not found")
        if len(parts) == 1:
            return self._listing([(s, True, layout.latest(files)) for s, files in sorted(seasons.items())])
        files = seasons.get(parts[1])
        if files is None:
            return self._send(404, b"not found")
        if len(parts) == 2:
            return self._listing([(name, False, key[2]) for name, key in sorted(files.items())])
        key = files.get(parts[2])
        if key is None or len(parts) > 3:
            return self._send(404, b"not found")
        entry = self.app.db.entry(key[0], key[1])
        video = self.app.db.video(key[1])
        if not entry or not video:
            return self._send(404, b"not found")
        return self._file(entry, video)

    def _file(self, entry, video):
        size = self.app.size(video)
        headers = {
            "Accept-Ranges": "bytes",
            "Last-Modified": formatdate(layout.published(video).timestamp(), usegmt=True),
            "Content-Type": "video/x-matroska",
        }
        if self.command == "HEAD":
            self.send_response(200)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            return

        # A reader that opened the file before the download finished still
        # believes the estimated size (rclone keeps it for the open handle)
        # and may seek anywhere inside it. So GETs are accepted up to the
        # larger of both sizes; past the real end they get padding.
        size = max(size, self.app.estimated_size(video))
        start, end = _parse_range(self.headers.get("Range"), size)
        if start is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        mode = self.app.mode(entry, video)
        if mode == "play":
            log.info(f"{video['id']}: play -> downloading ({video['title']})")
            self.app.cache.start(video)
            mode = "download"
        if mode == "download":
            # A finished file has Cues (seek index) and its final header; a
            # growing one has neither yet. ffmpeg-based readers (Plex's
            # transcoder) seek fine without Cues by reading forward, but some
            # direct-play clients (e.g. ExoPlayer) treat a Cue-less mkv as
            # unseekable. Downloads run at many MB/s, so short videos are
            # finished before the first byte goes out; long ones fall back to
            # the growing file after this wait.
            if self.app.cache.wait_done(video["id"], self.app.db.int_setting("wait_for_complete_secs", 20)):
                mode = "cached"

        self.send_response(206 if self.headers.get("Range") else 200)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(end - start + 1))
        if self.headers.get("Range"):
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        try:
            if mode == "stub":
                self._send_stub(video, start, end, size)
            else:
                self.app.cache.touch(video["id"])
                for chunk in self.app.cache.read(video, start, end, size):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except DownloadError as e:
            log.error(f"{video['id']}: {e}")
            self.close_connection = True

    def _send_stub(self, video, start, end, size):
        header = self.app.stub_header(video)
        limit = self.app.db.int_setting("probe_fallback_bytes", 32 * 1024 * 1024)
        pos, sent = start, 0
        while pos <= end:
            chunk_end = min(end, pos + CHUNK - 1)
            self.wfile.write(stub.read_range(header, size, pos, chunk_end))
            sent += chunk_end - pos + 1
            pos = chunk_end + 1
            if sent > limit and pos > len(header):
                log.warning(f"{video['id']}: {sent} bytes of stub streamed in one request -> "
                            f"treating as playback, starting download")
                self.app.cache.start(video)
                self.close_connection = True
                return


def _parse_range(header, size):
    if not header:
        return 0, size - 1
    m = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
    if not m:
        return 0, size - 1
    first, last = m.groups()
    if first == "":
        n = int(last or 0)
        return max(0, size - n), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size:
        return None, None
    return start, min(end, size - 1)


def serve(app, host="0.0.0.0", port=8080):
    Handler.app = app
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    log.info(f"listening on {host}:{port}")
    server.serve_forever()
