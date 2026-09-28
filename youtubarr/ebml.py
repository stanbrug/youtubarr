"""Minimal EBML (Matroska) reading and writing.

Only what the stub builder and the growing-file reader need: encode elements,
walk the children of a master element, and locate elements by id.
"""

import struct

# Element ids used here (ids keep their length-marker bits, as in the spec).
EBML = 0x1A45DFA3
SEGMENT = 0x18538067
SEEK_HEAD = 0x114D9B74
INFO = 0x1549A966
TRACKS = 0x1654AE6B
CLUSTER = 0x1F43B675
CUES = 0x1C53BB6B
TAGS = 0x1254C367
VOID = 0xEC
TIMESTAMP_SCALE = 0x2AD7B1
DURATION = 0x4489
MUXING_APP = 0x4D80
WRITING_APP = 0x5741
TITLE = 0x7BA9

UNKNOWN_SIZE_8 = b"\x01\xff\xff\xff\xff\xff\xff\xff"


def encode_id(element_id):
    length = (element_id.bit_length() + 7) // 8
    return element_id.to_bytes(length, "big")


def encode_size(size, length=None):
    """EBML variable-length size. length forces a width (1-8 bytes)."""
    if length is None:
        length = 1
        while size >= (1 << (7 * length)) - 1:
            length += 1
    if size >= (1 << (7 * length)) - 1:
        raise ValueError(f"size {size} does not fit in {length} bytes")
    return ((1 << (7 * length)) | size).to_bytes(length, "big")


def element(element_id, payload, size_length=None):
    return encode_id(element_id) + encode_size(len(payload), size_length) + payload


def uint_element(element_id, value):
    length = max(1, (value.bit_length() + 7) // 8)
    return element(element_id, value.to_bytes(length, "big"))


def float_element(element_id, value):
    return element(element_id, struct.pack(">d", value))


def string_element(element_id, value):
    return element(element_id, value.encode("utf-8"))


def void_element(total_size):
    """A Void element occupying exactly total_size bytes (>= 2)."""
    if total_size < 2:
        raise ValueError("a Void element needs at least 2 bytes")
    for size_length in range(1, 9):
        payload = total_size - 1 - size_length
        if payload >= 0 and payload < (1 << (7 * size_length)) - 1:
            return encode_id(VOID) + encode_size(payload, size_length) + b"\0" * payload
    raise ValueError("Void too large")


def void_header(total_size):
    """Header (id + 8-byte size) of a Void of total_size bytes, whose zero
    payload the caller produces lazily. total_size >= 9."""
    return encode_id(VOID) + encode_size(total_size - 9, 8)


def read_id(buf, pos):
    first = buf[pos]
    length = 1
    mask = 0x80
    while length <= 4 and not first & mask:
        mask >>= 1
        length += 1
    if length > 4:
        raise ValueError(f"invalid element id at {pos}")
    return int.from_bytes(buf[pos:pos + length], "big"), length


def read_size(buf, pos):
    """(size or None for 'unknown', width)."""
    first = buf[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8:
        raise ValueError(f"invalid size at {pos}")
    value = first & (mask - 1)
    for b in buf[pos + 1:pos + length]:
        value = (value << 8) | b
    if value == (1 << (7 * length)) - 1:
        return None, length
    return value, length


def children(buf, start, end):
    """Yield (id, element_start, data_start, data_end) for elements in
    buf[start:end]. An unknown-sized element extends to `end`."""
    pos = start
    while pos < end:
        if end - pos < 2:
            return
        element_id, id_len = read_id(buf, pos)
        size, size_len = read_size(buf, pos + id_len)
        data_start = pos + id_len + size_len
        data_end = end if size is None else data_start + size
        yield element_id, pos, data_start, data_end
        if size is None:
            return
        pos = data_end
