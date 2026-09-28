"""Download-on-play cache.

A play request starts one job per video:

    yt-dlp -J        -> format URLs (h264 video + m4a audio, capped height/bitrate)
    yt-dlp -o - (x2) -> video and audio bytes into two FIFOs (yt-dlp's own
                        chunked downloader, so YouTube doesn't throttle us)
    ffmpeg -c copy   -> <id>.part.mkv, written progressively
    rename           -> <id>.mkv once ffmpeg has finished (cues + real
                        duration written, header fixed up)

While the job runs, readers are served straight from the growing .part file
and block until the bytes they asked for exist. ffmpeg's Matroska muxer
reserves an 11-byte Void inside Info where it writes Duration at the very end;
until then we overlay that Void with a Duration element holding the duration
we already know from the catalog, so players see the real length from the
first byte.

Beyond the real end of the file (the advertised size is an upper-bound
estimate until the download finishes) readers get a Void element / zeros.
"""

import json
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time

from . import ebml

log = logging.getLogger("youtubarr.cache")

CHUNK = 256 * 1024


class DownloadError(Exception):
    pass


class Job:
    def __init__(self, video_id):
        self.video_id = video_id
        self.cond = threading.Condition()
        self.done = False
        self.error = None
        self.written = 0            # bytes currently in the .part file
        self.duration_offset = None  # offset of ffmpeg's reserved Duration Void
        self.started_at = time.time()


class Cache:
    def __init__(self, cache_dir, db):
        self.dir = cache_dir
        self.db = db
        os.makedirs(cache_dir, exist_ok=True)
        self._jobs = {}
        self._lock = threading.Lock()
        # Download slots; the limit is re-read every time, so changing
        # max_concurrent_downloads in the UI applies without a restart.
        self._slot_cond = threading.Condition()
        self._active = 0
        self.on_complete = None  # callback(video_id) after a finished download
        # Leftovers of jobs that died with the process are useless: their
        # header was never finalized.
        for name in os.listdir(cache_dir):
            if name.endswith(".part.mkv"):
                os.remove(os.path.join(cache_dir, name))

    # --- paths / state ------------------------------------------------------

    def final_path(self, video_id):
        return os.path.join(self.dir, f"{video_id}.mkv")

    def part_path(self, video_id):
        return os.path.join(self.dir, f"{video_id}.part.mkv")

    def cached_size(self, video_id):
        try:
            return os.path.getsize(self.final_path(video_id))
        except OSError:
            return None

    def job(self, video_id):
        with self._lock:
            job = self._jobs.get(video_id)
            return job if job and not (job.done and job.error) else None

    def wait_done(self, video_id, timeout):
        """Wait up to `timeout` seconds for a running download to finish.
        Returns True when the finished file is there."""
        job = self._jobs.get(video_id)
        if job is not None and timeout > 0:
            deadline = time.time() + timeout
            with job.cond:
                while not job.done and time.time() < deadline:
                    job.cond.wait(timeout=min(1.0, max(0.01, deadline - time.time())))
        return self.cached_size(video_id) is not None

    def touch(self, video_id):
        path = self.final_path(video_id)
        if os.path.exists(path):
            os.utime(path, None)

    # --- download -------------------------------------------------------------

    def start(self, video):
        """Start (or join) the download of `video` (a catalog row)."""
        video_id = video["id"]
        with self._lock:
            job = self._jobs.get(video_id)
            if job and not job.error:
                return job
            if self.cached_size(video_id) is not None:
                return None
            job = Job(video_id)
            self._jobs[video_id] = job
        threading.Thread(target=self._run, args=(job, video), daemon=True,
                         name=f"download-{video_id}").start()
        return job

    def _format_selector(self):
        max_height = self.db.int_setting("max_height", 1080)
        # Keep the real stream under the advertised bitrate so the final file
        # fits inside the size HEAD reported (audio ~130 kbps, mkv overhead).
        video_kbps = max(500, self.db.int_setting("estimated_bitrate_kbps", 10000) - 400)
        return (
            f"bv*[vcodec^=avc1][height<={max_height}][tbr<=?{video_kbps}]+ba[ext=m4a]"
            f"/b[vcodec^=avc1][acodec^=mp4a][height<={max_height}]"
        )

    def _ytdlp_base(self):
        cmd = ["yt-dlp", "--no-warnings", "--no-progress", "--no-playlist"]
        cookies = (self.db.setting("cookies_file") or "").strip()
        if cookies and os.path.exists(cookies):
            cmd += ["--cookies", cookies]
        return cmd

    def _run(self, job, video):
        video_id = video["id"]
        url = f"https://www.youtube.com/watch?v={video_id}"
        workdir = tempfile.mkdtemp(prefix=f"yt-{video_id}-", dir=self.dir)
        procs = []
        with self._slot_cond:
            while self._active >= max(1, self.db.int_setting("max_concurrent_downloads", 2)):
                self._slot_cond.wait(timeout=5)
            self._active += 1
        try:
            log.info(f"{video_id}: download starting")
            info_path = os.path.join(workdir, "info.json")
            result = subprocess.run(
                self._ytdlp_base() + ["-J", "-f", self._format_selector(), url],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode != 0:
                raise DownloadError(f"yt-dlp metadata failed: {result.stderr.strip()[-500:]}")
            info = json.loads(result.stdout)
            with open(info_path, "w") as f:
                json.dump(info, f)
            formats = info.get("requested_formats") or [info]
            log.info(f"{video_id}: formats {[f.get('format_id') for f in formats]} "
                     f"({info.get('width')}x{info.get('height')})")

            fifos = []
            for i, fmt in enumerate(formats):
                fifo = os.path.join(workdir, f"in{i}")
                os.mkfifo(fifo)
                fifos.append(fifo)

            part = self.part_path(video_id)
            ffmpeg_cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
            for fifo in fifos:
                ffmpeg_cmd += ["-i", fifo]
            if len(fifos) == 2:
                ffmpeg_cmd += ["-map", "0:v:0", "-map", "1:a:0"]
            ffmpeg_cmd += ["-c", "copy", "-map_metadata", "-1", "-f", "matroska", part]
            ffmpeg = subprocess.Popen(ffmpeg_cmd, stderr=subprocess.PIPE)
            procs.append(ffmpeg)

            for fifo, fmt in zip(fifos, formats):
                # --load-info-json reuses the URLs extracted above: no second
                # page/player request per stream. O_RDWR: opening a FIFO this
                # way never blocks, so a dead ffmpeg can't hang this thread in
                # open(); ffmpeg still sees EOF once yt-dlp exits.
                fd = os.open(fifo, os.O_RDWR)
                procs.append(subprocess.Popen(
                    self._ytdlp_base() + ["--load-info-json", info_path, "-f", fmt["format_id"], "-o", "-"],
                    stdout=fd, stderr=subprocess.PIPE,
                ))
                os.close(fd)

            while ffmpeg.poll() is None:
                self._update_progress(job, part)
                time.sleep(0.25)
            self._update_progress(job, part)
            for p in procs[1:]:
                p.wait(timeout=30)
            if ffmpeg.returncode != 0:
                raise DownloadError(f"ffmpeg failed: {ffmpeg.stderr.read().decode(errors='replace')[-500:]}")
            for p in procs[1:]:
                if p.returncode != 0:
                    raise DownloadError(f"yt-dlp stream failed: {p.stderr.read().decode(errors='replace')[-500:]}")

            os.replace(part, self.final_path(video_id))
            size = os.path.getsize(self.final_path(video_id))
            log.info(f"{video_id}: download complete, {size} bytes in {time.time() - job.started_at:.0f}s")
            with job.cond:
                job.written = size
                job.done = True
                job.cond.notify_all()
            self.cleanup()  # keep the cache within max_cache_gb right away
            if self.on_complete:
                self.on_complete(video_id)
        except Exception as e:
            log.error(f"{video_id}: download failed: {e}")
            for p in procs:
                if p.poll() is None:
                    p.kill()
            try:
                os.remove(self.part_path(video_id))
            except OSError:
                pass
            with job.cond:
                job.error = str(e)
                job.done = True
                job.cond.notify_all()
        finally:
            with self._slot_cond:
                self._active -= 1
                self._slot_cond.notify_all()
            shutil.rmtree(workdir, ignore_errors=True)

    def _update_progress(self, job, part):
        try:
            size = os.path.getsize(part)
        except OSError:
            return
        # 64 KiB is well past Info/Tracks, so a truncated header can't make
        # the lookup give up early.
        if job.duration_offset is None and size >= 64 * 1024:
            job.duration_offset = self._find_duration_void(part)
        if size != job.written:
            with job.cond:
                job.written = size
                job.cond.notify_all()

    @staticmethod
    def _find_duration_void(path):
        """Offset of the 11-byte Void ffmpeg reserves for Duration in Info,
        or -1 if the layout isn't what we expect (then nothing is patched)."""
        with open(path, "rb") as f:
            buf = f.read(64 * 1024)
        try:
            for element_id, _s, data_start, data_end in ebml.children(buf, 0, len(buf)):
                if element_id != ebml.SEGMENT:
                    continue
                for child_id, _cs, c_data, c_end in ebml.children(buf, data_start, min(data_end, len(buf))):
                    if child_id == ebml.INFO:
                        for info_id, i_start, _id, i_end in ebml.children(buf, c_data, c_end):
                            if info_id == ebml.DURATION:
                                return -1  # already has a real Duration
                            if info_id == ebml.VOID and i_end - i_start == 11:
                                return i_start
                        return -1
        except (ValueError, IndexError):
            pass
        return -1

    # --- reading ----------------------------------------------------------------

    def read(self, video, start, end, total_size):
        """Yield bytes [start, end] of the video, from the finished file or the
        growing one, padding past the real end up to total_size (the size the
        reader was told). Blocks while bytes are still being downloaded.
        Raises DownloadError if the download fails."""
        video_id = video["id"]
        pos = start
        duration_patch = ebml.encode_id(ebml.DURATION) + b"\x88" + struct.pack(">d", video["duration"] * 1000.0)
        f = None
        try:
            while pos <= end:
                job = self._jobs.get(video_id)
                final = self.final_path(video_id)
                if f is None:
                    if os.path.exists(final):
                        f = open(final, "rb")
                    elif job is not None and not job.done:
                        with job.cond:
                            while job.written <= pos and not job.done:
                                job.cond.wait(timeout=5)
                        if job.error:
                            raise DownloadError(job.error)
                        f = open(final if job.done else self.part_path(video_id), "rb")
                    else:
                        raise DownloadError("not cached and no download running")

                growing = job is not None and not job.done
                if growing:
                    with job.cond:
                        while job.written <= pos and not job.done:
                            job.cond.wait(timeout=5)
                    if job.error:
                        raise DownloadError(job.error)
                    available = job.written
                    patch_at = job.duration_offset if job.duration_offset and job.duration_offset > 0 else None
                else:
                    available = os.fstat(f.fileno()).st_size
                    patch_at = None

                if pos >= available:
                    if growing:
                        continue
                    while pos <= end:
                        chunk = _padding(pos, min(end, pos + CHUNK - 1), available, total_size)
                        yield chunk
                        pos += len(chunk)
                    return

                f.seek(pos)
                data = f.read(min(CHUNK, end + 1 - pos, available - pos))
                if not data:
                    continue
                if patch_at is not None and pos < patch_at + 11 and pos + len(data) > patch_at:
                    data = _overlay(data, pos, patch_at, duration_patch)
                yield data
                pos += len(data)
        finally:
            if f is not None:
                f.close()

    # --- housekeeping ---------------------------------------------------------------

    def remove(self, video_id):
        """Drop a finished cached file (video removed from every subscription)."""
        job = self._jobs.get(video_id)
        if job is not None and not job.done:
            return False
        try:
            os.remove(self.final_path(video_id))
            log.info(f"{video_id}: removed from cache (video removed)")
            return True
        except OSError:
            return False

    def downloads(self):
        with self._lock:
            return [{"video_id": j.video_id, "bytes": j.written, "since": j.started_at}
                    for j in self._jobs.values() if not j.done]

    # A file read within this window counts as "in use" and is never evicted
    # (someone is probably watching it right now).
    IN_USE_SECS = 15 * 60

    def usage(self):
        """(bytes used by finished + growing files, number of finished files)."""
        total, count = 0, 0
        for name in os.listdir(self.dir):
            if name.endswith(".mkv"):
                try:
                    total += os.path.getsize(os.path.join(self.dir, name))
                except OSError:
                    continue
                if not name.endswith(".part.mkv"):
                    count += 1
        return total, count

    def cleanup(self):
        """Two passes: drop files not watched for cache_days (0 = keep by
        age), then evict least-recently-watched files until the cache fits
        max_cache_gb (0 = no size limit). mtime is the last-access time
        (touch() on every read). Running downloads and files in use stay."""
        max_age = self.db.int_setting("cache_days", 7) * 86400
        max_bytes = self.db.int_setting("max_cache_gb", 50) * 1024 ** 3
        now = time.time()
        files = []
        for name in os.listdir(self.dir):
            if not name.endswith(".mkv") or name.endswith(".part.mkv"):
                continue
            path = os.path.join(self.dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, name[:-4], path))

        removed = []
        keep = []
        for mtime, size, video_id, path in files:
            if max_age > 0 and now - mtime > max_age and now - mtime > self.IN_USE_SECS:
                removed.append((video_id, path, f"not watched for {max_age // 86400} days"))
            else:
                keep.append((mtime, size, video_id, path))

        if max_bytes > 0:
            running = sum(os.path.getsize(os.path.join(self.dir, n)) for n in os.listdir(self.dir)
                          if n.endswith(".part.mkv"))
            used = running + sum(size for _m, size, _v, _p in keep)
            for mtime, size, video_id, path in sorted(keep):  # oldest access first
                if used <= max_bytes:
                    break
                if now - mtime < self.IN_USE_SECS:
                    continue
                removed.append((video_id, path, f"cache over {max_bytes // 1024 ** 3} GB"))
                used -= size

        for video_id, path, reason in removed:
            try:
                os.remove(path)
                log.info(f"{video_id}: removed from cache ({reason})")
            except OSError as e:
                log.warning(f"{video_id}: could not remove cached file: {e}")
        return len(removed)


def _overlay(data, pos, patch_at, patch):
    buf = bytearray(data)
    for i, b in enumerate(patch):
        j = patch_at + i - pos
        if 0 <= j < len(buf):
            buf[j] = b
    return bytes(buf)


def _padding(pos, end, real_size, total_size):
    """Bytes [pos, end] of the filler between the real end of a finished file
    and the advertised size: one top-level Void element spanning exactly that
    gap (legal after a Segment), i.e. its 9-byte header followed by zeros."""
    gap = total_size - real_size
    header = ebml.void_header(gap) if gap >= 9 else b""
    out = bytearray()
    for offset in range(pos, min(end, pos + len(header)) + 1):
        i = offset - real_size
        if 0 <= i < len(header):
            out.append(header[i])
        else:
            break
    return bytes(out) + b"\0" * (end + 1 - pos - len(out))
