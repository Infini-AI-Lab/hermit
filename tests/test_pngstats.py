"""pngstats: the stdlib PNG decoder (all five filters, colour types 0/2/3/4/6) and its three measures, on PNGs the test
encodes itself with zlib — no PIL anywhere."""
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


def encode(width: int, height: int, rows: list[bytes], colour: int, filters, *, palette: bytes = b"",
           interlace: int = 0, depth: int = 8) -> bytes:
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
    ihdr = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, interlace)
    body = chunk(b"IHDR", ihdr) + (chunk(b"PLTE", palette) if palette else b"")
    return pngstats.SIGNATURE + body + chunk(b"IDAT", zlib.compress(bytes(out))) + chunk(b"IEND", b"")


def solid(width: int, height: int, rgb: tuple[int, int, int], colour: int = 2) -> list[bytes]:
    pixel = bytes(rgb) if colour == 2 else bytes(rgb) + b"\xff"
    return [pixel * width] * height


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


def lanes(rows: list[bytes], colour: int, palette: bytes = b"") -> tuple[bytes, bytes, bytes]:
    bpp = CHANNELS[colour]
    data = b"".join(rows)
    if colour == 3:
        return tuple(bytes(palette[i * 3 + k] for i in data) for k in range(3))
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


def test_palette_images_decode_through_plte(tmp_path):
    palette = bytes((10, 20, 30, 200, 100, 0, 0, 0, 255))
    rows = [bytes([0, 1, 2, 1] * 25)] * 30 + [bytes([2] * 100)] * 5
    path = write(tmp_path / "pal.png", encode(100, 35, rows, 3, [1, 2, 4, 3, 0] * 7, palette=palette))
    image = pngstats.decode(path)
    assert (image.r, image.g, image.b) == lanes(rows, 3, palette)
    result = pngstats.stats(path)
    assert result["dominant_rgb"] == [200, 100, 0] and result["error"] is None   # index 1: 1500 of 3500 pixels; index 2: 1250


def test_stats_of_a_solid_frame_is_blank(tmp_path):
    path = write(tmp_path / "solid.png", encode(320, 240, solid(320, 240, (30, 120, 200)), 2, 2))
    result = pngstats.stats(path)
    assert result == {"width": 320, "height": 240, "mean_luma": round(0.299 * 30 + 0.587 * 120 + 0.114 * 200, 3),
                      "nonuniform_fraction": 0.0, "dominant_rgb": [30, 120, 200], "blank": True, "error": None}


def test_stats_counts_pixels_far_from_the_dominant_colour(tmp_path):
    rows = solid(400, 300, (50, 50, 50), colour=6)
    # 3 % of the pixels differ by exactly 9 in one channel (far), another 3 % by 8 (near: still the dominant colour)
    far = bytes((59, 50, 50, 255)) * 400
    near = bytes((50, 58, 50, 0)) * 400
    rows = [far] * 9 + [near] * 9 + rows[18:]
    path = write(tmp_path / "mixed.png", encode(400, 300, rows, 6, [4, 3, 1, 2, 0] * 60))
    result = pngstats.stats(path)
    assert result["dominant_rgb"] == [50, 50, 50] and abs(result["nonuniform_fraction"] - 0.03) < 1e-9
    assert result["blank"] is False
    expected_luma = 0.299 * (50 + 9 * 0.03) + 0.587 * (50 + 8 * 0.03) + 0.114 * 50
    assert abs(result["mean_luma"] - expected_luma) < 6e-4            # mean_luma is rounded to 3 decimals


def test_small_or_structured_images_follow_the_blank_rule(tmp_path):
    small = write(tmp_path / "small.png", encode(64, 64, pattern(64, 64, 2), 2, 4))
    assert pngstats.stats(small)["blank"] is True            # structured but narrower than 320x240
    big = write(tmp_path / "big.png", encode(320, 240, pattern(320, 240, 2), 2, 1))
    result = pngstats.stats(big)
    assert result["blank"] is False and result["nonuniform_fraction"] > 0.9


def test_unsupported_and_broken_files_report_an_error(tmp_path):
    rows = solid(8, 8, (1, 2, 3))
    cases = {
        "interlaced.png": encode(8, 8, rows, 2, 0, interlace=1),
        "depth16.png": encode(8, 8, rows, 2, 0, depth=16),
        "notpng.png": b"\xff\xd8\xff" + bytes(100),
        "truncated.png": encode(8, 8, rows, 2, 0)[:-20],
        "short_idat.png": pngstats.SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
                          + chunk(b"IDAT", zlib.compress(bytes(50))) + chunk(b"IEND", b""),
        "badcrc.png": encode(8, 8, rows, 2, 0)[:-1] + b"\x00",
        "missing.png": None,
    }
    for name, data in cases.items():
        path = tmp_path / name
        if data is not None:
            path.write_bytes(data)
        result = pngstats.stats(path)
        assert result["error"] and result["blank"] is True and result["width"] is None, name
    assert "interlaced" in pngstats.stats(tmp_path / "interlaced.png")["error"]
    big = write(tmp_path / "toobig.png", encode(100, 100, solid(100, 100, (0, 0, 0)), 2, 0))
    assert "exceeds" in pngstats.stats(big, max_pixels=9_999)["error"]
    assert pngstats.stats(big)["error"] is None
    assert pngstats.diff_fraction(big, tmp_path / "notpng.png") is None
    assert pngstats.thumb_corr(big, tmp_path / "missing.png") == 0.0


def test_diff_fraction_uses_the_capturer_rule(tmp_path):
    base = solid(50, 40, (100, 100, 100))
    a = write(tmp_path / "a.png", encode(50, 40, base, 2, 2))
    assert pngstats.diff_fraction(a, a) == 0.0
    # row 0: +2 in one channel (not changed); row 1: -3 in one channel (changed); row 2: wraps past 255 (changed)
    rows = [bytes((102, 100, 100)) * 50, bytes((100, 97, 100)) * 50, bytes((100, 100, 254)) * 50] + base[3:]
    b = write(tmp_path / "b.png", encode(50, 40, rows, 2, 4))
    assert abs(pngstats.diff_fraction(a, b) - 100 / 2000) < 1e-12
    other = write(tmp_path / "c.png", encode(50, 41, base + [base[0]], 2, 0))
    assert pngstats.diff_fraction(a, other) is None


def test_thumb_corr_measures_shared_structure(tmp_path):
    rows = pattern(128, 80, 2)
    a = write(tmp_path / "a.png", encode(128, 80, rows, 2, 1))
    inverted = [bytes(255 - v for v in row) for row in rows]
    b = write(tmp_path / "b.png", encode(128, 80, inverted, 2, 3))
    flat = write(tmp_path / "flat.png", encode(64, 40, solid(64, 40, (9, 9, 9)), 2, 0))
    shifted = [bytes(min(255, v + 40) for v in row) for row in rows]      # brighter, same structure, other size ok
    c = write(tmp_path / "c.png", encode(128, 80, shifted, 2, 2))
    assert abs(pngstats.thumb_corr(a, a) - 1.0) < 1e-9
    assert pngstats.thumb_corr(a, b) < -0.95
    assert pngstats.thumb_corr(a, flat) == 0.0
    assert pngstats.thumb_corr(a, c) > 0.95
    assert pngstats.thumb_corr(a, c, size=(16, 10)) > 0.95
