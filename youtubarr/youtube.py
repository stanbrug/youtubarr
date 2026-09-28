"""YouTube Data API v3 client.

Quota (default 10,000 units/day, reset at midnight Pacific time): every call
used here costs 1 unit, except search.list (100), which is only used to
resolve legacy /c/<name> URLs. Units are counted per day in the database.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger("youtubarr.youtube")

API = os.environ.get("YOUTUBARR_YOUTUBE_API_BASE", "https://www.googleapis.com/youtube/v3")
COSTS = {"search": 100}

_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)


class YouTubeError(Exception):
    pass


def parse_duration(iso):
    """ISO 8601 duration (PT1H2M3S, P1DT2H) -> seconds; 0 for live/unknown."""
    m = _DURATION_RE.match(iso or "")
    if not m:
        return 0.0
    d, h, mi, s = (float(m.group(k) or 0) for k in ("d", "h", "m", "s"))
    return d * 86400 + h * 3600 + mi * 60 + s


def parse_input(text):
    """What the user pasted -> ("channel"|"playlist"|"handle"|"username"|"custom", value)."""
    text = (text or "").strip()
    if not text:
        raise YouTubeError("empty input")
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", text):
        return "channel", text
    if re.fullmatch(r"(PL|UU|OL|FL|LL|RD)[A-Za-z0-9_-]{10,}", text):
        return "playlist", text
    if text.startswith("@"):
        return "handle", text
    if "://" not in text and ("youtube.com" in text or "youtu.be" in text):
        text = "https://" + text
    url = urlparse(text)
    if url.netloc:
        query = parse_qs(url.query)
        if "list" in query:
            return "playlist", query["list"][0]
        parts = [p for p in url.path.split("/") if p]
        if parts:
            if parts[0].startswith("@"):
                return "handle", parts[0]
            if parts[0] == "channel" and len(parts) > 1:
                return "channel", parts[1]
            if parts[0] == "user" and len(parts) > 1:
                return "username", parts[1]
            if parts[0] == "c" and len(parts) > 1:
                return "custom", parts[1]
            if parts[0] not in ("watch", "playlist", "shorts", "live", "results"):
                return "custom", parts[0]
    raise YouTubeError(f"not a channel or playlist: {text}")


def pacific_day():
    # Quota resets at midnight America/Los_Angeles; UTC-8 is close enough for
    # a counter (DST shifts it by an hour).
    return datetime.fromtimestamp(time.time() - 8 * 3600, timezone.utc).strftime("%Y-%m-%d")


def best_thumbnail(thumbs, order=("maxres", "standard", "high", "medium", "default")):
    for key in order:
        if thumbs.get(key, {}).get("url"):
            return thumbs[key]["url"]
    return ""


class YouTube:
    def __init__(self, db):
        self.db = db
        self.session = requests.Session()

    def _get(self, resource, **params):
        key = (self.db.setting("youtube_api_key") or "").strip()
        if not key:
            raise YouTubeError("no YouTube API key set (Settings)")
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["key"] = key
        for attempt in range(3):
            try:
                resp = self.session.get(f"{API}/{resource}", params=params, timeout=30)
            except requests.RequestException as e:
                if attempt == 2:
                    raise YouTubeError(f"{resource}: {e}") from e
                time.sleep(2 * (attempt + 1))
                continue
            self.db.add_quota(pacific_day(), COSTS.get(resource, 1))
            if resp.status_code == 200:
                return resp.json()
            try:
                err = resp.json().get("error", {})
                reason = (err.get("errors") or [{}])[0].get("reason", "")
                message = err.get("message", resp.text[:200])
            except ValueError:
                reason, message = "", resp.text[:200]
            if resp.status_code >= 500 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                raise YouTubeError("YouTube API quota exceeded for today")
            raise YouTubeError(f"{resource}: HTTP {resp.status_code} {reason} {message}".strip())
        raise YouTubeError(f"{resource}: failed")

    # --- channels / playlists -----------------------------------------------------

    def channel(self, **selector):
        """channels.list by id / forHandle / forUsername -> normalized dict or None."""
        data = self._get("channels", part="snippet,contentDetails,brandingSettings", maxResults=1, **selector)
        items = data.get("items") or []
        if not items:
            return None
        c = items[0]
        snippet = c.get("snippet", {})
        return {
            "channel_id": c["id"],
            "title": snippet.get("title", c["id"]),
            "description": snippet.get("description", ""),
            "avatar_url": best_thumbnail(snippet.get("thumbnails", {}), ("high", "medium", "default")),
            "banner_url": (c.get("brandingSettings", {}).get("image", {}) or {}).get("bannerExternalUrl", ""),
            "uploads": c.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", ""),
        }

    def playlist(self, playlist_id):
        data = self._get("playlists", part="snippet", id=playlist_id, maxResults=1)
        items = data.get("items") or []
        if not items:
            return None
        snippet = items[0].get("snippet", {})
        return {
            "playlist_id": playlist_id,
            "title": snippet.get("title", playlist_id),
            "description": snippet.get("description", ""),
            "channel_id": snippet.get("channelId", ""),
            "thumbnail": best_thumbnail(snippet.get("thumbnails", {})),
        }

    def resolve(self, text):
        """User input -> subscription fields (without folder/mode)."""
        kind, value = parse_input(text)
        if kind == "playlist":
            pl = self.playlist(value)
            if pl is None:
                raise YouTubeError(f"playlist {value} not found (private?)")
            owner = self.channel(id=pl["channel_id"]) if pl["channel_id"] else None
            return {
                "kind": "playlist", "source_id": value, "playlist_id": value,
                "channel_id": pl["channel_id"], "title": pl["title"],
                "description": pl["description"] or (owner or {}).get("description", ""),
                "avatar_url": pl["thumbnail"] or (owner or {}).get("avatar_url", ""),
                "banner_url": (owner or {}).get("banner_url", ""),
            }
        if kind == "channel":
            ch = self.channel(id=value)
        elif kind == "handle":
            ch = self.channel(forHandle=value)
        elif kind == "username":
            ch = self.channel(forUsername=value)
        else:
            found = self._get("search", part="snippet", type="channel", q=value, maxResults=1)
            items = found.get("items") or []
            ch = self.channel(id=items[0]["snippet"]["channelId"]) if items else None
        if ch is None or not ch["uploads"]:
            raise YouTubeError(f"channel not found: {text}")
        return {
            "kind": "channel", "source_id": ch["channel_id"], "playlist_id": ch["uploads"],
            "channel_id": ch["channel_id"], "title": ch["title"], "description": ch["description"],
            "avatar_url": ch["avatar_url"], "banner_url": ch["banner_url"],
        }

    # --- playlist items / videos ---------------------------------------------------------

    def playlist_items(self, playlist_id, max_pages=None, stop_when_known=None):
        """Yield (video_id, position, is_unavailable) in playlist order.
        stop_when_known(ids) -> True ends paging once a whole page is known
        (hourly checks only look at the newest uploads)."""
        token = None
        pages = 0
        while True:
            data = self._get("playlistItems", part="snippet,contentDetails,status",
                             playlistId=playlist_id, maxResults=50, pageToken=token)
            ids = []
            for item in data.get("items", []):
                vid = item.get("contentDetails", {}).get("videoId")
                if not vid:
                    continue
                privacy = item.get("status", {}).get("privacyStatus", "public")
                title = item.get("snippet", {}).get("title", "")
                unavailable = privacy == "private" or title in ("Private video", "Deleted video")
                ids.append(vid)
                yield vid, item.get("snippet", {}).get("position"), unavailable
            pages += 1
            token = data.get("nextPageToken")
            if not token or (max_pages and pages >= max_pages):
                return
            if stop_when_known and stop_when_known(ids):
                return

    def videos(self, ids):
        """videos.list in batches of 50 -> {id: normalized video or None}."""
        out = {}
        ids = list(dict.fromkeys(ids))
        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            data = self._get("videos", part="snippet,contentDetails,status", id=",".join(batch), maxResults=50)
            found = {}
            for item in data.get("items", []):
                found[item["id"]] = self._normalize(item)
            for vid in batch:
                out[vid] = found.get(vid)
        return out

    @staticmethod
    def _normalize(item):
        snippet = item.get("snippet", {})
        details = item.get("contentDetails", {})
        status = item.get("status", {})
        live = snippet.get("liveBroadcastContent", "none")
        duration = parse_duration(details.get("duration"))
        playable = (
            status.get("privacyStatus") in ("public", "unlisted")
            and status.get("uploadStatus", "processed") == "processed"
            and live == "none"
            and duration > 0
        )
        hd = details.get("definition") == "hd"
        return {
            "id": item["id"],
            "channel_id": snippet.get("channelId", ""),
            "channel_title": snippet.get("channelTitle", ""),
            "title": snippet.get("title", item["id"]),
            "description": snippet.get("description", ""),
            "published_at": snippet.get("publishedAt") or datetime.now(timezone.utc).isoformat(),
            "duration": duration,
            # No resolution in the API; YouTube is overwhelmingly 16:9. The
            # stub carries this guess until Plex re-analyzes the real file.
            "width": 1920 if hd else 854,
            "height": 1080 if hd else 480,
            "thumbnails": {k: v.get("url") for k, v in snippet.get("thumbnails", {}).items() if v.get("url")},
            "playable": playable,
            "live": live,
        }
