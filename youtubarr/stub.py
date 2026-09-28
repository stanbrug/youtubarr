"""Stub Matroska files for videos that aren't cached.

Plex analyzes every new file (codecs, duration, resolution). For a video we
haven't downloaded, we answer that analysis with a synthetic file that costs
no YouTube request:

    EBML header | Segment( Info(Duration = real duration) | Tracks | one tiny
    Cluster ) | Void ........................................ | (virtual size)

Tracks and the Cluster come from a template ffmpeg encodes once per
resolution (a single black h264 frame + a short aac frame), so codec ids and
CodecPrivate are exactly what a real h264/aac mkv carries. The Void pads the
file to the same virtual size HEAD reports; its payload is zeros, produced
on the fly, never stored. A demuxer reads the header, finds the streams and
the duration, and skips the Void by seeking.

The same layout is what the real cached file will be (h264 + aac in mkv, see
cache.py), so Plex's stored media info carries over.
"""

import os
import subprocess
import threading

from . import ebml

AUDIO_RATE = 44100  # YouTube's m4a (itag 140) is AAC-LC 44.1 kHz stereo


class StubBuilder:
    def __init__(self, template_dir):
        self.template_dir = template_dir
        self._lock = threading.Lock()
        self._templates = {}  # (w, h) -> (ebml_header, tracks, cluster)

    def _template_path(self, width, height):
        return os.path.join(self.template_dir, f"template_{width}x{height}.mkv")

    def _make_template(self, path, width, height):
        os.makedirs(self.template_dir, exist_ok=True)
        tmp = path + ".tmp.mkv"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color=black:s={width}x{height}:r=30:d=0.04",
                "-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo",
                "-t", "0.04",
                "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "128k",
                "-map_metadata", "-1", "-f", "matroska", tmp,
            ],
            check=True,
        )
        os.replace(tmp, path)

    def _load_template(self, width, height):
        key = (width, height)
        with self._lock:
            if key in self._templates:
                return self._templates[key]
            path = self._template_path(width, height)
            if not os.path.exists(path):
                self._make_template(path, width, height)
            with open(path, "rb") as f:
                buf = f.read()

            ebml_header = tracks = cluster = None
            for element_id, start, data_start, data_end in ebml.children(buf, 0, len(buf)):
                if element_id == ebml.EBML:
                    ebml_header = buf[start:data_end]
                elif element_id == ebml.SEGMENT:
                    for child_id, c_start, _c_data, c_end in ebml.children(buf, data_start, data_end):
                        if child_id == ebml.TRACKS and tracks is None:
                            tracks = buf[c_start:c_end]
                        elif child_id == ebml.CLUSTER and cluster is None:
                            cluster = buf[c_start:c_end]
            if not (ebml_header and tracks and cluster):
                raise RuntimeError(f"unexpected template layout in {path}")
            self._templates[key] = (ebml_header, tracks, cluster)
            return self._templates[key]

    def header(self, duration_secs, total_size, width, height, title=""):
        """Bytes from offset 0 up to (and including) the Void's header. The
        remaining total_size - len(header) bytes are zeros."""
        ebml_header, tracks, cluster = self._load_template(width, height)
        info = ebml.element(
            ebml.INFO,
            ebml.uint_element(ebml.TIMESTAMP_SCALE, 1_000_000)
            + ebml.float_element(ebml.DURATION, float(duration_secs) * 1000.0)
            + ebml.string_element(ebml.MUXING_APP, "youtubarr-stub")
            + ebml.string_element(ebml.WRITING_APP, "youtubarr-stub")
            + (ebml.string_element(ebml.TITLE, title[:200]) if title else b""),
        )
        body = info + tracks + cluster
        segment_head_len = 4 + 8  # Segment id + 8-byte size
        void_total = total_size - len(ebml_header) - segment_head_len - len(body)
        if void_total < 9:
            raise ValueError("virtual size too small for the stub")
        segment_size = len(body) + void_total
        return (
            ebml_header
            + ebml.encode_id(ebml.SEGMENT) + ebml.encode_size(segment_size, 8)
            + body
            + ebml.void_header(void_total)
        )


def read_range(header, total_size, start, end):
    """Bytes [start, end] (inclusive) of a stub whose header is `header`."""
    end = min(end, total_size - 1)
    if start > end:
        return b""
    out = bytearray()
    if start < len(header):
        out += header[start:min(end + 1, len(header))]
    zeros = end + 1 - max(start, len(header))
    if zeros > 0:
        out += b"\0" * zeros
    return bytes(out)
