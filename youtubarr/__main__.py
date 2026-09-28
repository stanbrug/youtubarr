import logging
import os
import subprocess
import threading
import time

from .cache import Cache
from .db import Database
from .plex import PlexWorker
from .server import App, RingLog, serve
from .stub import StubBuilder
from .sync import Syncer

DATA = os.environ.get("YOUTUBARR_DATA", "/data")
log = logging.getLogger("youtubarr")


def _update_ytdlp_forever():
    """YouTube changes often; a stale yt-dlp is the most common failure."""
    while True:
        try:
            result = subprocess.run(["pip", "install", "-q", "-U", "--root-user-action=ignore", "yt-dlp"],
                                    timeout=300, capture_output=True, text=True)
            version = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True).stdout.strip()
            log.info(f"yt-dlp {version}" + ("" if result.returncode == 0 else " (update failed)"))
        except Exception as e:
            log.warning(f"yt-dlp update failed: {e}")
        time.sleep(24 * 3600)


def _cleanup_forever(cache):
    while True:
        time.sleep(3600)
        try:
            cache.cleanup()
        except Exception as e:
            log.warning(f"cache cleanup failed: {e}")


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ring = RingLog()
    ring.setLevel(logging.INFO)
    logging.getLogger().addHandler(ring)

    db = Database(os.path.join(DATA, "youtubarr.db"))
    cache = Cache(os.path.join(DATA, "cache"), db)
    app = App(db, cache, StubBuilder(os.path.join(DATA, "templates")))
    app.ring = ring
    app.plex = PlexWorker(app)
    app.sync = Syncer(app)
    cache.on_complete = app.plex.request_analyze

    if os.environ.get("YOUTUBARR_AUTO_UPDATE", "1") == "1":
        threading.Thread(target=_update_ytdlp_forever, daemon=True, name="ytdlp-update").start()
    threading.Thread(target=_cleanup_forever, args=(cache,), daemon=True, name="cache-cleanup").start()
    app.plex.start()
    app.sync.start()
    serve(app, port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
