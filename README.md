# Youtubarr

YouTube channels and playlists in Plex, on demand, without downloading ahead.

```
Plex → rclone HTTP mount → youtubarr (HTTP) → stub | cache ← yt-dlp
                                   ↑
                     YouTube Data API v3 (metadata)
```

- **Subscriptions** (web UI): a channel (URL, `@handle`, `UC…` id) or a
  playlist (`PL…` / playlist URL). Shown in Plex either as
  *channel mode* (season = upload year, episode = `MMDDNN`) or
  *playlist as series* (season 1, episode = position). Optional minimum
  duration (e.g. 181 s to skip Shorts). Live/upcoming, private and
  unprocessed videos are skipped.
- **Sync**: every hour the newest uploads (1 API unit + 1 per 50 new videos),
  every 24 h a full pass that detects removed/private videos and takes them out
  of the listing and Plex. API units are counted per day (Status tab).
- **Virtual files**: `/<Show> [UCid]/Season YYYY/<Show> - sYYYYeMMDDNN - Title [videoId].mkv`.
  Numbers and filenames are fixed when a video is first seen. `HEAD` reports a
  stable size (duration × estimated bitrate).
- **Stub**: until Plex has analyzed a file, reads get a real Matroska header
  (h264 + AAC tracks, real duration) and padding. No YouTube request.
- **Play**: once Plex has analyzed a file, a read is a play only if Plex
  confirms it: `/status/sessions` must show a session for that video (polled
  for up to *playback_confirm_secs*, default 8). Scans, re-analysis and other
  Plex background reads get the stub and never reach YouTube
  (*require_plex_session*, default on). For a confirmed play, yt-dlp fetches
  h264 + m4a (up to the max height/bitrate), ffmpeg muxes them into the cache
  while downloading. The first read waits up to *wait_for_complete_secs* for
  the finished file (with Cues for seeking); longer videos stream from the
  growing file. When the download is done, Plex is told to re-analyze, so it
  learns the real resolution and bitrate.
- **Plex**: partial scans per show folder, then per episode title, cleaned
  summary and air date, and per show title, summary (channel/playlist
  description), poster (avatar) and art (banner) — all locked. Posters are
  URLs Plex fetches itself (`i.ytimg.com` maxresdefault, else hqdefault); none
  are stored. Everything goes through one background worker at a steady rate.
- **Descriptions**: URLs, sponsor/affiliate lines (configurable keywords),
  the social-media block and hashtags at the end, and chapter timestamps are
  removed; blank lines collapsed; cut at ~1000 characters. Global rules, per
  subscription overrides, live preview. The original stays in SQLite; changing
  rules re-pushes everything without YouTube API calls.
- **Cache**: max size (GB) and "not watched for N days" cleanup, evicting the
  least recently watched first; files being downloaded or watched stay.
  Max concurrent downloads, optional cookies file, yt-dlp auto-update.
- **Settings** live in the web UI and SQLite (API key, Plex URL/token/library,
  cache, downloads, rules, optional UI password). `YOUTUBARR_<SETTING>` env
  vars only seed them on first start.

## Install (Docker)

```bash
docker compose up -d --build
```

`docker-compose.yml` runs youtubarr (web UI on port 8080) and
`youtubarr-mount`, an rclone container that mounts the library at
`/mnt/youtubarr` on the host.

**Ubuntu hosts:** the AppArmor profile for `/usr/bin/fusermount3` also applies
inside containers and makes every rclone mount fail with *Permission denied*.
The mount service works around it by running a copy of `fusermount3` from
another path (see the compose file).

Then:

1. Give the Plex container the mount: `- /mnt/youtubarr:/mnt/youtubarr:rslave`.
2. In Plex, add a **TV Shows** library with agent **Plex Personal Media**
   (`tv.plex.agents.none`, the current successor of "Personal Media Shows")
   and scanner **Plex TV Series**, folder `/mnt/youtubarr`.
3. Plex → Settings → Troubleshooting: enable **Allow media deletion** (needed to
   remove deleted videos and unsubscribed shows).
4. Turn off the Plex settings below.
5. Open `http://<host>:8080/` → *Instellingen*: YouTube Data API key, Plex URL
   and token, "Verbinding testen" to pick the library, and the mount path as
   Plex sees it (`/mnt/youtubarr`).
6. *Abonnementen*: add channels/playlists.

A YouTube Data API key: Google Cloud console → new project → enable
"YouTube Data API v3" → Credentials → API key. The default quota is
10,000 units per day, far more than hourly checks need.

## Plex settings that must be off

Anything that reads whole files would download every video.

| Setting (Settings → Library / Scheduled Tasks) | Pref | Value |
|---|---|---|
| Generate video preview thumbnails | `GenerateBIFBehavior` | never |
| Generate chapter thumbnails | `GenerateChapterThumbBehavior` | never |
| Generate intro video markers | `GenerateIntroMarkerBehavior` | never |
| Generate credits video markers | `GenerateCreditsMarkerBehavior` | never |
| Generate ad / voice activity markers | `GenerateAdMarkerBehavior`, `GenerateVADBehavior` | never |
| Analyze audio tracks for loudness | `LoudnessAnalysisBehavior` | never |
| Perform extensive media analysis during maintenance | `ButlerTaskDeepMediaAnalysis` | off |
| Upgrade media analysis during maintenance | `ButlerTaskUpgradeMediaAnalysis` | off |

Also switch them off in the YouTube library itself (Edit library → Advanced:
preview thumbnails, intro/credits/ad markers, voice activity, loudness), so
turning one on globally for other libraries doesn't reach YouTube.

Periodic library scans may stay on: an unchanged file isn't re-read. Even if
a setting slips, reads without a Plex playback session never start a download
(see *Play* above).

## Test results

Proof of concept (stub + playback, real Plex 1.43.4, rclone 1.75):
Plex analyzed three stubs in 20 s with correct codecs/resolution/duration and
**0** YouTube requests; direct play first bytes 8–11 s, seeks < 0.5 s; Plex's
transcoder starting at 05:00 in a cold video: first segment after 5.6 s.

End to end (`tests/e2e`, real Plex, real downloads, YouTube Data API mock):

| Step | Result |
|---|---|
| Add channel + playlist | 3 + 2 videos in 3 s (Short and upcoming live skipped), 8 API units |
| Plex: match + analyze stubs | 27 s |
| Metadata, posters, show poster/art pushed and locked | 6 s later |
| New upload on the channel | in Plex with metadata and poster after 51 s |
| Play | first MB after 11 s; seek instant; Plex re-analyzed to 1920x818 / 1.66 Mbps |
| Video deleted on YouTube | gone from Plex after 3 s |
| Rule change | all summaries re-pushed after 12 s, no API calls |
| Unsubscribe | show removed from Plex after 3 s |

Unit tests: `python -m unittest discover -s tests` (the ffmpeg stub test runs
inside the image).

## Known limits

- The stub's resolution is a guess (API gives only hd/sd → 1920x1080 or
  854x480). Until the first play re-analyzes the file, a transcode scales to
  that guess; a 2.4:1 film is stretched once.
- A direct-play client that seeks *by byte ratio* during the very first play
  can overshoot, because Plex still has the estimated size. Clients that seek
  by the file's Cues (almost all) are fine.
- Plex ignores a partial scan of a folder it has never seen; new shows
  therefore trigger one full scan of the library.
- Videos that need a login (members-only, age-restricted) need a cookies file.

## Layout of the code

| File | What |
|---|---|
| `youtubarr/youtube.py` | Data API client, input parsing, quota counting |
| `youtubarr/sync.py` | quick/full checks, numbering, removals |
| `youtubarr/layout.py` | folder/file naming |
| `youtubarr/server.py` | rclone listing, stub vs play decision (heuristic documented there), API |
| `youtubarr/stub.py`, `ebml.py` | stub Matroska files |
| `youtubarr/cache.py` | download-on-play, growing-file reads, cache limits |
| `youtubarr/plex.py` | Plex worker: scans, matching, metadata/posters, deletes, re-analysis |
| `youtubarr/describe.py` | description cleanup |
| `youtubarr/ui.html` | web UI |
| `poc/` | the proof of concept scripts |
| `tests/e2e/` | end-to-end environment (mock API, throwaway Plex) |
