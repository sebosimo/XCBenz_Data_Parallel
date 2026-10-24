"""Lossless int16 prediction and independently compressed, range-readable tiles."""

import gzip
import hashlib
import json
import struct
import zlib

import numpy as np


MAGIC = b"XCBWVP2\0"
HEADER = struct.Struct("<8sII")
TILE_SIZE = 256
HALO = 1
OVERVIEW_STRIDE = 4
TRANSFORM = "delta2d-zigzag-byteplanes-v1"


def compress(values):
    """Predict in both dimensions, zigzag signed residuals, then shuffle bytes."""
    data = np.asarray(values, dtype="<i2").astype(np.int32)
    delta = data.copy()
    delta[:, 1:] = data[:, 1:] - data[:, :-1]
    delta[1:] -= delta[:-1].copy()
    signed = delta.astype(np.uint16).view(np.int16).astype(np.int32)
    zigzag = ((signed << 1) ^ (signed >> 15)).astype("<u2")
    shuffled = zigzag.view(np.uint8).reshape(-1, 2).T.copy().tobytes()
    return gzip.compress(shuffled, compresslevel=6, mtime=0)


def decompress(payload, width, height):
    expected = width * height * 2
    decoder = zlib.decompressobj(wbits=31)
    data = decoder.decompress(payload, expected + 1)
    if len(data) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("invalid compressed W block length or trailing data")
    planes = np.frombuffer(data, dtype=np.uint8).reshape(2, -1)
    zigzag = planes[0].astype(np.uint16) | (planes[1].astype(np.uint16) << 8)
    signed = ((zigzag.astype(np.int32) >> 1) ^ -(zigzag.astype(np.int32) & 1)).reshape(height, width)
    # Modular arithmetic makes this exact even across missing-value boundaries.
    values = signed.cumsum(axis=0, dtype=np.int64).cumsum(axis=1, dtype=np.int64)
    return values.astype("<u2").view("<i2")


def package(values):
    values = np.asarray(values, dtype="<i2")
    height, width = values.shape
    chunks = []
    offset = 0

    def block(data):
        nonlocal offset
        payload = compress(data)
        result = {"offset": offset, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                  "width": data.shape[1], "height": data.shape[0], "decoded_bytes": data.size * 2}
        chunks.append(payload)
        offset += len(payload)
        return result

    overview = {"stride": OVERVIEW_STRIDE, "block": block(values[::OVERVIEW_STRIDE, ::OVERVIEW_STRIDE])}
    padded = np.pad(values, HALO, mode="edge")
    tiles = []
    for y in range(0, height, TILE_SIZE):
        for x in range(0, width, TILE_SIZE):
            core_width, core_height = min(TILE_SIZE, width - x), min(TILE_SIZE, height - y)
            tiles.append({"x": x, "y": y, "core_width": core_width, "core_height": core_height,
                          "block": block(padded[y:y + core_height + 2 * HALO, x:x + core_width + 2 * HALO])})
    directory = {"schema": "xcbenz.w.tiles.v2", "width": width, "height": height,
                 "tile_size": TILE_SIZE, "halo": HALO, "transform": TRANSFORM,
                 "compression": "gzip", "overview": overview, "tiles": tiles}
    index = json.dumps(directory, sort_keys=True, separators=(",", ":")).encode()
    return HEADER.pack(MAGIC, 2, len(index)) + index + b"".join(chunks), {
        "bytes": len(index), "sha256": hashlib.sha256(index).hexdigest(), "header_bytes": HEADER.size,
        "initial_bytes": HEADER.size + len(index) + overview["block"]["bytes"],
    }


def read_directory(payload):
    magic, version, length = HEADER.unpack_from(payload)
    if magic != MAGIC or version != 2 or length > 1024 * 1024:
        raise ValueError("invalid W package header")
    return json.loads(payload[HEADER.size:HEADER.size + length]), HEADER.size + length


def read_block(payload, entry, body_offset):
    start = body_offset + entry["offset"]
    block = payload[start:start + entry["bytes"]]
    if hashlib.sha256(block).hexdigest() != entry["sha256"]:
        raise ValueError("W tile checksum mismatch")
    return decompress(block, entry["width"], entry["height"])
