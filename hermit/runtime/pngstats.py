"""PNG statistics without PIL: a small standard-library decoder (zlib and the five scanline filters) and the measures
the visual showcase needs from a capture — is it blank, how much changed between two captures, how alike two frames are.

Supported: bit depth 8, colour types 0 (grey), 2 (RGB), 3 (palette), 4 (grey + alpha), 6 (RGBA), no interlace; alpha
is ignored. `stats` reports anything else in its `error` field (and `blank: True`) instead of raising, because callers
count an undecodable capture as nothing seen. The filters run lane by lane (one lane per channel) so the alpha lane is
skipped; the Up filter and the pixel comparisons use whole-row big-integer arithmetic, which keeps a 1280x800 capture
under a second without numpy.
"""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
from itertools import accumulate
from pathlib import Path
import struct
import sys
from typing import NamedTuple
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
COLOUR_LANES = {0: (0,), 2: (0, 1, 2), 3: (0,), 4: (0,), 6: (0, 1, 2)}   # the lanes the statistics read: never alpha
MAX_PIXELS = 2_200_000     # why: the host capture is 1280x800 (1.0 Mpx); the agent harness refuses images above 2.1 Mpx
BLANK_FRACTION = 0.02      # why: a solid frame with only a control bar differs from its dominant colour on < 2 % of it
NEAR = 8                   # a channel delta up to this still counts as the dominant colour (anti-aliasing, dithering)
DIFF_DELTA = 2             # why: the host capturer counts a pixel as changed when any channel moves by more than 2
MIN_WIDTH, MIN_HEIGHT = 320, 240
LUMA = (0.299, 0.587, 0.114)


class PngError(ValueError):
    """The file is not a PNG this decoder reads."""


class Image(NamedTuple):
    width: int
    height: int
    r: bytes      # one byte per pixel, row-major
    g: bytes
    b: bytes


# --- decoding ---------------------------------------------------------------------------------------------------------
def decode(path: str | Path, max_pixels: int = MAX_PIXELS) -> Image:
    """The RGB lanes of a PNG file. Raises PngError (a ValueError) for anything outside the supported subset."""
    data = Path(path).read_bytes()
    if not data.startswith(SIGNATURE):
        raise PngError("not a PNG file")
    header, palette, idat, pos = None, b"", [], len(SIGNATURE)
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        crc = data[pos + 8 + length:pos + 12 + length]
        if len(body) != length or len(crc) != 4:
            raise PngError("truncated chunk")
        if zlib.crc32(kind + body) & 0xFFFFFFFF != struct.unpack(">I", crc)[0]:
            raise PngError(f"bad CRC in chunk {kind.decode('latin-1')}")
        pos += 12 + length
        if kind == b"IHDR":
            if length != 13:
                raise PngError("malformed IHDR")
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if header is None:
        raise PngError("no IHDR chunk")
    width, height, depth, colour, compression, filter_method, interlace = header
    if colour not in CHANNELS:
        raise PngError(f"unsupported colour type {colour}")
    if depth != 8:
        raise PngError(f"unsupported bit depth {depth} (only 8)")
    if interlace != 0:
        raise PngError("interlaced PNG is not supported")
    if compression != 0 or filter_method != 0:
        raise PngError("unsupported compression or filter method")
    if width == 0 or height == 0:
        raise PngError("empty image")
    if width * height > max_pixels:
        raise PngError(f"{width}x{height} exceeds {max_pixels} pixels")
    bpp = CHANNELS[colour]
    expected = height * (width * bpp + 1)
    try:
        raw = zlib.decompressobj().decompress(b"".join(idat), expected + 1)
    except zlib.error as exc:
        raise PngError(f"IDAT does not inflate: {exc}") from exc
    if len(raw) != expected:
        raise PngError(f"IDAT holds {len(raw)} bytes, expected {expected}")
    rows = _unfilter(raw, width, height, bpp, COLOUR_LANES[colour])
    pixels = b"".join(rows)
    if colour == 3:
        if len(palette) < 3:
            raise PngError("palette image without PLTE")
        table = palette.ljust(768, b"\x00")     # why: an index past the palette is undefined by the spec; read as black
        lanes = tuple(pixels.translate(table[k::3]) for k in range(3))
    else:
        lanes = tuple(pixels[k::bpp] for k in COLOUR_LANES[colour])
        if len(lanes) == 1:
            lanes = (lanes[0], lanes[0], lanes[0])
    return Image(width, height, *lanes)


def _unfilter(raw: bytes, width: int, height: int, bpp: int, lanes: tuple[int, ...]) -> list[bytes]:
    """Reverse the per-scanline filters. Lanes are independent under every filter, so only `lanes` are reconstructed
    exactly; the others (alpha) carry garbage that nothing reads."""
    stride = width * bpp
    rows: list[bytes] = []
    prev = bytes(stride)
    pos = 0
    for _ in range(height):
        kind = raw[pos]
        line = raw[pos + 1:pos + 1 + stride]
        pos += 1 + stride
        if kind == 0:
            cur = line
        elif kind == 1:
            cur = _sub(line, bpp, lanes)
        elif kind == 2:
            cur = _add_mod256(line, prev)
        elif kind == 3:
            cur = _average(line, prev, bpp, lanes)
        elif kind == 4:
            cur = _paeth(line, prev, bpp, lanes)
        else:
            raise PngError(f"unknown filter type {kind}")
        rows.append(cur)
        prev = cur
    return rows


def _add8(a: int, b: int) -> int:
    return (a + b) & 255


def _sub(line: bytes, bpp: int, lanes: tuple[int, ...]) -> bytes:
    out = bytearray(line)
    for j in lanes:
        out[j::bpp] = bytes(accumulate(line[j::bpp], _add8))
    return bytes(out)


def _average(line: bytes, prev: bytes, bpp: int, lanes: tuple[int, ...]) -> bytes:
    out = bytearray(line)
    for j in lanes:
        res, left = bytearray(), 0
        for x, up in zip(line[j::bpp], prev[j::bpp]):
            left = (x + ((left + up) >> 1)) & 255
            res.append(left)
        out[j::bpp] = res
    return bytes(out)


def _paeth(line: bytes, prev: bytes, bpp: int, lanes: tuple[int, ...]) -> bytes:
    out = bytearray(line)
    for j in lanes:
        res, a, c = bytearray(), 0, 0
        for x, b in zip(line[j::bpp], prev[j::bpp]):
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            a = (x + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
            res.append(a)
            c = b
        out[j::bpp] = res
    return bytes(out)


@lru_cache(maxsize=16)
def _masks(n: int) -> tuple[int, int, int, int]:
    """Bit masks over an n-byte little-endian integer: the even bytes, the odd bytes, and a 256 in every even / odd
    byte slot. Adding or subtracting two rows one parity at a time keeps each byte's carry in the neighbouring slot,
    where the mask discards it — a whole row per big-integer operation instead of a Python loop per byte."""
    pairs = n // 2 + 1
    even = int.from_bytes((b"\xff\x00" * pairs)[:n], "little")
    odd = (even << 8) & ((1 << (8 * n)) - 1)
    ones_even = int.from_bytes(b"\x00\x01" * pairs, "little")
    return even, odd, ones_even, ones_even << 8


def _add_mod256(x: bytes, y: bytes) -> bytes:
    """(x[i] + y[i]) & 255 for every byte."""
    n = len(x)
    even, odd, _, _ = _masks(n)
    a, b = int.from_bytes(x, "little"), int.from_bytes(y, "little")
    total = (((a & even) + (b & even)) & even) | (((a & odd) + (b & odd)) & odd)
    return total.to_bytes(n, "little")


def _sub_mod256(x: bytes, y: bytes) -> bytes:
    """(x[i] - y[i]) & 255 for every byte."""
    n = len(x)
    even, odd, ones_even, ones_odd = _masks(n)
    a, b = int.from_bytes(x, "little"), int.from_bytes(y, "little")
    diff = ((((a & even) + ones_even) - (b & even)) & even) | ((((a & odd) + ones_odd) - (b & odd)) & odd)
    return diff.to_bytes(n, "little")


# --- measures ---------------------------------------------------------------------------------------------------------
def _flags(lane: bytes, table: bytes) -> int:
    """One bit per pixel (as a big integer) after a 256-entry lookup; OR-able across lanes, countable with bit_count."""
    return int.from_bytes(lane.translate(table), "little")


def _far_table(centre: int, near: int) -> bytes:
    return bytes(1 if abs(v - centre) > near else 0 for v in range(256))


_CHANGED = bytes(1 if DIFF_DELTA < v < 256 - DIFF_DELTA else 0 for v in range(256))   # a (x - y) & 255 delta beyond ±2


def _dominant(img: Image) -> tuple[int, int, int]:
    """The most frequent (r, g, b): pack each pixel into a native 32-bit word so counting runs in C."""
    packed = bytearray(img.width * img.height * 4)
    packed[0::4], packed[1::4], packed[2::4] = img.r, img.g, img.b
    value = Counter(memoryview(packed).cast("I")).most_common(1)[0][0]
    raw = value.to_bytes(4, sys.byteorder)
    return raw[0], raw[1], raw[2]


def stats(path: str | Path, max_pixels: int = MAX_PIXELS) -> dict:
    """{width, height, mean_luma, nonuniform_fraction, dominant_rgb, blank, error}. `nonuniform_fraction` is the share
    of pixels that differ by more than NEAR in any channel from the dominant colour; `blank` is True when that share is
    below BLANK_FRACTION, when the image is smaller than 320x240, or when it cannot be decoded (see `error`)."""
    try:
        img = decode(path, max_pixels)
    except (ValueError, OSError, struct.error, zlib.error) as exc:
        return {"width": None, "height": None, "mean_luma": None, "nonuniform_fraction": None, "dominant_rgb": None,
                "blank": True, "error": f"{type(exc).__name__}: {exc}"}
    n = img.width * img.height
    dominant = _dominant(img)
    far = _flags(img.r, _far_table(dominant[0], NEAR)) | _flags(img.g, _far_table(dominant[1], NEAR)) \
        | _flags(img.b, _far_table(dominant[2], NEAR))
    fraction = far.bit_count() / n
    luma = (LUMA[0] * sum(img.r) + LUMA[1] * sum(img.g) + LUMA[2] * sum(img.b)) / n
    return {"width": img.width, "height": img.height, "mean_luma": round(luma, 3),
            "nonuniform_fraction": round(fraction, 6), "dominant_rgb": list(dominant),
            "blank": fraction < BLANK_FRACTION or img.width < MIN_WIDTH or img.height < MIN_HEIGHT, "error": None}


def diff_fraction(a: str | Path, b: str | Path, max_pixels: int = MAX_PIXELS) -> float | None:
    """The share of pixels whose r, g or b moved by more than DIFF_DELTA between the two files (the host capturer's
    rule); None when the sizes differ or either file does not decode."""
    try:
        x, y = decode(a, max_pixels), decode(b, max_pixels)
    except (ValueError, OSError, struct.error, zlib.error):
        return None
    if (x.width, x.height) != (y.width, y.height):
        return None
    changed = _flags(_sub_mod256(x.r, y.r), _CHANGED) | _flags(_sub_mod256(x.g, y.g), _CHANGED) \
        | _flags(_sub_mod256(x.b, y.b), _CHANGED)
    return changed.bit_count() / (x.width * x.height)


def _thumb(img: Image, size: tuple[int, int]) -> list[float]:
    """Mean luma per cell of a size[0] x size[1] grid of boxes over the image (box downscale)."""
    tw, th = size
    w, h = img.width, img.height
    out = []
    for j in range(th):
        y0, y1 = j * h // th, max((j + 1) * h // th, j * h // th + 1)
        for i in range(tw):
            x0, x1 = i * w // tw, max((i + 1) * w // tw, i * w // tw + 1)
            sr = sg = sb = 0
            for y in range(min(y0, h - 1), min(y1, h)):
                start, stop = y * w + min(x0, w - 1), y * w + min(x1, w)
                sr += sum(img.r[start:stop]); sg += sum(img.g[start:stop]); sb += sum(img.b[start:stop])
            count = max(1, (min(y1, h) - min(y0, h - 1)) * (min(x1, w) - min(x0, w - 1)))
            out.append((LUMA[0] * sr + LUMA[1] * sg + LUMA[2] * sb) / count)
    return out


def thumb_corr(a: str | Path, b: str | Path, size: tuple[int, int] = (64, 40), max_pixels: int = MAX_PIXELS) -> float:
    """Pearson correlation of the two files' grey thumbnails (box-downscaled to `size`); the sizes may differ. 0.0 when
    either thumbnail is flat (no structure to correlate) or either file does not decode."""
    try:
        ta, tb = _thumb(decode(a, max_pixels), size), _thumb(decode(b, max_pixels), size)
    except (ValueError, OSError, struct.error, zlib.error):
        return 0.0
    n = len(ta)
    ma, mb = sum(ta) / n, sum(tb) / n
    cov = sum((p - ma) * (q - mb) for p, q in zip(ta, tb))
    va, vb = sum((p - ma) ** 2 for p in ta), sum((q - mb) ** 2 for q in tb)
    if va <= 1e-9 or vb <= 1e-9:
        return 0.0
    return max(-1.0, min(1.0, cov / (va * vb) ** 0.5))
