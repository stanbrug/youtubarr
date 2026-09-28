"""SQLite storage.

subscriptions  a channel or playlist that becomes one show in Plex
videos         one row per YouTube video (metadata as YouTube returns it;
               the original description is never modified)
entries        one row per file Plex sees: a video inside a subscription,
               with its fixed filename/season/episode and its Plex state.
               The same video in two subscriptions is two entries (two
               files, two Plex items).
settings       key/value, edited in the web UI
quota          YouTube Data API units used per (Pacific) day
"""

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,            -- 'channel' | 'playlist'
    source_id       TEXT NOT NULL UNIQUE,     -- UC... or PL...
    playlist_id     TEXT NOT NULL,            -- uploads playlist (UU...) or the playlist itself
    channel_id      TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    avatar_url      TEXT NOT NULL DEFAULT '',
    banner_url      TEXT NOT NULL DEFAULT '',
    folder          TEXT NOT NULL UNIQUE,     -- show folder, fixed at creation
    mode            TEXT NOT NULL,            -- 'channel' (season = year) | 'playlist' (season 1, episode = index)
    enabled         INTEGER NOT NULL DEFAULT 1,
    min_duration    INTEGER NOT NULL DEFAULT 0,   -- skip videos shorter than this (seconds), e.g. Shorts
    rules           TEXT NOT NULL DEFAULT '{}',   -- per-subscription description rule overrides
    last_check_at   REAL,
    last_full_at    REAL,
    last_error      TEXT,
    plex_show_key   TEXT,
    show_pushed_at  REAL,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    channel_title TEXT NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    published_at  TEXT NOT NULL,
    duration      REAL NOT NULL,
    width         INTEGER NOT NULL DEFAULT 1920,
    height        INTEGER NOT NULL DEFAULT 1080,
    thumbnails    TEXT NOT NULL DEFAULT '{}',
    removed       INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    subscription_id  INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    video_id         TEXT NOT NULL REFERENCES videos(id),
    season           INTEGER NOT NULL,
    episode          INTEGER NOT NULL,
    season_folder    TEXT NOT NULL,
    filename         TEXT NOT NULL,
    position         INTEGER,                 -- playlist position (playlist mode)
    removed          INTEGER NOT NULL DEFAULT 0,
    plex_key         TEXT,                    -- Plex ratingKey of the episode
    plex_analyzed_at REAL,                    -- Plex analyzed the (stub) file
    meta_pushed_at   REAL,
    poster_pushed_at REAL,
    plex_deleted_at  REAL,
    added_at         REAL NOT NULL,
    PRIMARY KEY (subscription_id, video_id)
);
CREATE INDEX IF NOT EXISTS entries_video ON entries(video_id);
CREATE INDEX IF NOT EXISTS entries_plex ON entries(plex_key);
CREATE TABLE IF NOT EXISTS quota (
    day   TEXT PRIMARY KEY,
    units INTEGER NOT NULL
);
"""

DEFAULT_RULES = {
    "urls": True,
    "sponsor": True,
    "social": True,
    "hashtags": True,
    "timestamps": True,
    "max_length": 1000,
    "sponsor_keywords": [
        "sponsored by", "sponsor", "use code", "promo code", "discount code", "affiliate",
        "check out", "link in bio", "gesponsord", "kortingscode", "met code", "partnerlink",
    ],
}

# Env vars YOUTUBARR_<KEY> only seed these on first start; after that the web
# UI (settings table) is the source of truth.
DEFAULT_SETTINGS = {
    "youtube_api_key": "",
    "plex_url": "",
    "plex_token": "",
    "plex_section_id": "",
    "plex_path_prefix": "/mnt/youtubarr",   # the rclone mount as Plex sees it
    "check_interval_minutes": "60",
    "full_check_hours": "24",
    "plex_rate_per_second": "2",
    # HEAD size = duration x this, and the download is capped below it so the
    # real file always fits. Plex also uses it for bandwidth decisions until
    # the first play (10000 made Plex ask 21 Mbps for direct play).
    "estimated_bitrate_kbps": "8000",
    "max_height": "1080",
    "max_concurrent_downloads": "2",
    "wait_for_complete_secs": "20",
    "cache_days": "7",
    "max_cache_gb": "50",
    "cookies_file": "",
    "probe_fallback_bytes": str(32 * 1024 * 1024),
    # Only download when Plex reports a playback session for the video, so
    # scans and background analysis never reach YouTube.
    "require_plex_session": "1",
    "playback_confirm_secs": "8",
    "ui_password": "",
    "rules": json.dumps(DEFAULT_RULES),
}

SECRET_SETTINGS = ("youtube_api_key", "plex_token", "ui_password")


class Database:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._local = threading.local()
        self.write_lock = threading.RLock()
        conn = self.conn()
        conn.executescript(SCHEMA)
        for key, default in DEFAULT_SETTINGS.items():
            seed = os.environ.get(f"YOUTUBARR_{key.upper()}", default)
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, seed))

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def q(self, sql, args=()):
        return [dict(r) for r in self.conn().execute(sql, args)]

    def one(self, sql, args=()):
        row = self.conn().execute(sql, args).fetchone()
        return dict(row) if row else None

    def x(self, sql, args=()):
        with self.write_lock:
            return self.conn().execute(sql, args)

    # --- settings -------------------------------------------------------------

    def settings(self):
        return {r["key"]: r["value"] for r in self.q("SELECT key, value FROM settings")}

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def int_setting(self, key, default):
        try:
            return int(float(self.setting(key, default)))
        except (TypeError, ValueError):
            return default

    def float_setting(self, key, default):
        try:
            return float(self.setting(key, default))
        except (TypeError, ValueError):
            return default

    def rules(self):
        try:
            rules = json.loads(self.setting("rules") or "{}")
        except ValueError:
            rules = {}
        return {**DEFAULT_RULES, **rules}

    def update_settings(self, values):
        with self.write_lock:
            for key, value in values.items():
                if key not in DEFAULT_SETTINGS:
                    continue
                if key == "rules" and not isinstance(value, str):
                    value = json.dumps(value)
                self.conn().execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )

    # --- quota ----------------------------------------------------------------

    def add_quota(self, day, units):
        self.x("INSERT INTO quota(day, units) VALUES (?, ?) "
               "ON CONFLICT(day) DO UPDATE SET units = units + excluded.units", (day, units))

    def quota(self, day):
        row = self.one("SELECT units FROM quota WHERE day = ?", (day,))
        return row["units"] if row else 0

    # --- subscriptions --------------------------------------------------------------

    def subscriptions(self):
        return self.q("SELECT * FROM subscriptions ORDER BY title COLLATE NOCASE")

    def subscription(self, sub_id):
        return self.one("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))

    def subscription_by_folder(self, folder):
        return self.one("SELECT * FROM subscriptions WHERE folder = ?", (folder,))

    def add_subscription(self, sub):
        with self.write_lock:
            cur = self.conn().execute(
                """INSERT INTO subscriptions(kind, source_id, playlist_id, channel_id, title, description,
                       avatar_url, banner_url, folder, mode, min_duration, created_at)
                   VALUES (:kind, :source_id, :playlist_id, :channel_id, :title, :description,
                       :avatar_url, :banner_url, :folder, :mode, :min_duration, :created_at)""",
                {"min_duration": 0, **sub, "created_at": time.time()},
            )
            return cur.lastrowid

    def update_subscription(self, sub_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k} = :{k}" for k in fields)
        self.x(f"UPDATE subscriptions SET {cols} WHERE id = :id", {**fields, "id": sub_id})

    def delete_subscription(self, sub_id):
        with self.write_lock:
            self.conn().execute("DELETE FROM entries WHERE subscription_id = ?", (sub_id,))
            self.conn().execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))

    # --- videos / entries ---------------------------------------------------------

    def upsert_video(self, v):
        self.x(
            """INSERT INTO videos(id, channel_id, channel_title, title, description, published_at,
                                  duration, width, height, thumbnails, removed, updated_at)
               VALUES (:id, :channel_id, :channel_title, :title, :description, :published_at,
                       :duration, :width, :height, :thumbnails, 0, :updated_at)
               ON CONFLICT(id) DO UPDATE SET
                   channel_title = excluded.channel_title, title = excluded.title,
                   description = excluded.description, published_at = excluded.published_at,
                   duration = excluded.duration, width = excluded.width, height = excluded.height,
                   thumbnails = excluded.thumbnails, removed = 0, updated_at = excluded.updated_at""",
            {**v, "thumbnails": json.dumps(v.get("thumbnails", {})), "updated_at": time.time()},
        )

    def video(self, video_id):
        v = self.one("SELECT * FROM videos WHERE id = ?", (video_id,))
        if v:
            v["thumbnails"] = json.loads(v["thumbnails"] or "{}")
        return v

    def entry(self, sub_id, video_id):
        return self.one("SELECT * FROM entries WHERE subscription_id = ? AND video_id = ?", (sub_id, video_id))

    def entries(self, sub_id, include_removed=False):
        sql = "SELECT * FROM entries WHERE subscription_id = ?" + ("" if include_removed else " AND removed = 0")
        return self.q(sql + " ORDER BY season, episode", (sub_id,))

    def update_entry(self, sub_id, video_id, **fields):
        cols = ", ".join(f"{k} = :{k}" for k in fields)
        self.x(f"UPDATE entries SET {cols} WHERE subscription_id = :sub AND video_id = :vid",
               {**fields, "sub": sub_id, "vid": video_id})
