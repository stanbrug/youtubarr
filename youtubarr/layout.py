"""Virtual folder layout Plex sees.

Channel mode:   /<Show> [<UCid>]/Season <YYYY>/<Show> - s<YYYY>e<MMDDNN> - <Title> [<videoId>].mkv
                season = upload year, episode = month+day of the upload plus a
                2-digit counter for several uploads on one day. Assigned once
                when a video is first seen, so numbers never shift.
Playlist mode:  /<Playlist> [<PLid>]/Season 01/<Playlist> - s01e<NNN> - <Title> [<videoId>].mkv
                episode = position in the playlist (1-based), follows reordering.

Filenames are fixed when an entry is created; a later title change on YouTube
only changes the metadata pushed to Plex, never the file.
"""

import re
from datetime import datetime

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
VIDEO_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.mkv$")


def safe(name, limit=120):
    name = _UNSAFE.sub("", name or "").strip().rstrip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:limit].rstrip() or "untitled"


def show_folder(title, source_id):
    return f"{safe(title, 80)} [{source_id}]"


def published(video):
    return datetime.fromisoformat(video["published_at"].replace("Z", "+00:00"))


def channel_slot(video, taken):
    """(season, episode, season_folder) for a new channel-mode entry.
    taken: set of episode numbers already used in that season."""
    when = published(video)
    base = when.month * 10000 + when.day * 100
    for n in range(1, 100):
        if base + n not in taken:
            return when.year, base + n, f"Season {when.year}"
    return None  # more than 99 uploads on one day


def filename(show_title, season, episode, mode, video):
    if mode == "playlist":
        tag = f"s01e{episode:03d}"
    else:
        tag = f"s{season}e{episode:06d}"
    return f"{safe(show_title, 60)} - {tag} - {safe(video['title'])} [{video['id']}].mkv"


def build(db):
    """{show_folder: {season_folder: {filename: (subscription_id, video_id, published_ts)}}}"""
    tree = {s["folder"]: {} for s in db.q("SELECT id, folder FROM subscriptions")}
    rows = db.q(
        """SELECT s.folder, e.season_folder, e.filename, e.subscription_id, e.video_id, v.published_at
           FROM entries e JOIN subscriptions s ON s.id = e.subscription_id
           JOIN videos v ON v.id = e.video_id
           WHERE e.removed = 0 AND v.removed = 0"""
    )
    for r in rows:
        tree.setdefault(r["folder"], {}).setdefault(r["season_folder"], {})[r["filename"]] = (
            r["subscription_id"], r["video_id"], published(r).timestamp(),
        )
    return tree


def latest(node):
    """Newest publish time inside a show or season node: a stable directory
    mtime that only changes when a video is added (Plex rescans a folder
    whose mtime changed)."""
    if isinstance(node, tuple):
        return node[2]
    stamps = [latest(child) for child in node.values()]
    return max(stamps) if stamps else 0
