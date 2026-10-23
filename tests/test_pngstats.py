"""pngstats: the stdlib PNG decoder (all five filters, colour types 0/2/4/6) and the blank rule of its stats, on PNGs
the test encodes itself with zlib — no PIL anywhere."""
from __future__ import annotations

from pathlib import Path
import struct
import zlib

from hermit.runtime import pngstats

CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def encode(width: int, height: int, rows: list[bytes], colour: int, filters) -> bytes:
    """A PNG from raw scanlines; `filters` is one filter type per row (or one for all). The forward filters here are
    the straightforward per-byte definitions from the PNG specification, independent of the decoder's tricks."""
    bpp = CHANNELS[colour]
    kinds = list(filters) if not isinstance(filters, int) else [filters] * height
    out, prev = bytearray(), bytes(width * bpp)
    for row, kind in zip(rows, kinds):
        left = lambda i: row[i - bpp] if i >= bpp else 0            # noqa: E731
        upleft = lambda i: prev[i - bpp] if i >= bpp else 0         # noqa: E731
        if kind == 0:
            data = row
        elif kind == 1:
            data = bytes((row[i] - left(i)) & 255 for i in range(len(row)))
        elif kind == 2:
            data = bytes((row[i] - prev[i]) & 255 for i in range(len(row)))
        elif kind == 3:
            data = bytes((row[i] - ((left(i) + prev[i]) >> 1)) & 255 for i in range(len(row)))
        else:
            data = bytes((row[i] - paeth(left(i), prev[i], upleft(i))) & 255 for i in range(len(row)))
        out += bytes([kind]) + data
        prev = row
    ihdr = struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0)
    return pngstats.SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(out))) + chunk(b"IEND", b"")


def solid(width: int, height: int, rgb: tuple[int, int, int]) -> list[bytes]:
    return [bytes(rgb) * width] * height


def pattern(width: int, height: int, colour: int) -> list[bytes]:
    """A deterministic, non-uniform image in the given colour type (alpha channels vary too, to prove they are
    ignored)."""
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            r, g, b = (x * 7 + y) & 255, (x ^ y) & 255, (y * 3 + x // 2) & 255
            if colour == 2:
                row += bytes((r, g, b))
            elif colour == 6:
                row += bytes((r, g, b, (x + y) & 255))
            elif colour == 0:
                row += bytes((r,))
            elif colour == 4:
                row += bytes((r, (x * 5) & 255))
        rows.append(bytes(row))
    return rows


def lanes(rows: list[bytes], colour: int) -> tuple[bytes, bytes, bytes]:
    bpp = CHANNELS[colour]
    data = b"".join(rows)
    if bpp in (1, 2):
        return (data[0::bpp],) * 3
    return data[0::bpp], data[1::bpp], data[2::bpp]


def write(path: Path, png: bytes) -> Path:
    path.write_bytes(png)
    return path


def test_every_filter_and_colour_type_decodes_exactly(tmp_path):
    for colour in (0, 2, 4, 6):
        rows = pattern(97, 23, colour)       # odd width: lanes of odd length exercise the parity masks
        for filters in (0, 1, 2, 3, 4, [0, 1, 2, 3, 4] * 4 + [4, 3, 2]):
            path = write(tmp_path / f"c{colour}.png", encode(97, 23, rows, colour, filters))
            image = pngstats.decode(path)
            assert (image.width, image.height) == (97, 23)
            assert (image.r, image.g, image.b) == lanes(rows, colour), (colour, filters)


def test_stats_of_a_solid_frame_is_blank(tmp_path):
    path = write(tmp_path / "solid.png", encode(320, 240, solid(320, 240, (30, 120, 200)), 2, 2))
    result = pngstats.stats(path)
    assert result == {"width": 320, "height": 240, "mean_luma": round(0.299 * 30 + 0.587 * 120 + 0.114 * 200, 3),
                      "nonuniform_fraction": 0.0, "dominant_rgb": [30, 120, 200], "blank": True, "error": None}


def test_small_or_structured_images_follow_the_blank_rule(tmp_path):
    small = write(tmp_path / "small.png", encode(64, 64, pattern(64, 64, 2), 2, 4))
    assert pngstats.stats(small)["blank"] is True            # structured but narrower than 320x240
    big = write(tmp_path / "big.png", encode(320, 240, pattern(320, 240, 2), 2, 1))
    result = pngstats.stats(big)
    assert result["blank"] is False and result["nonuniform_fraction"] > 0.9
