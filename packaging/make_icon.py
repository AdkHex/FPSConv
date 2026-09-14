"""Generate assets/icon.png and assets/icon.ico without any imaging library.

A dark rounded square with a light "play" triangle and two speed bars — plain
pixel maths, written as PNG (zlib) and wrapped into a PNG-in-ICO container,
which Windows Vista+ reads natively.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 256
BG = (20, 20, 20)
FG = (236, 236, 236)
ACCENT = (92, 184, 255)


def _inside_rounded(x: float, y: float, s: int, r: float) -> bool:
    cx = min(max(x, r), s - r)
    cy = min(max(y, r), s - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _inside_triangle(x: float, y: float, s: int) -> bool:
    # play triangle: points (0.36,0.30) (0.36,0.70) (0.70,0.50) in unit space
    ax, ay, bx, by, cx, cy = 0.36 * s, 0.30 * s, 0.36 * s, 0.70 * s, 0.70 * s, 0.50 * s
    d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by)
    d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy)
    d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def render(s: int) -> bytes:
    rows = []
    ss = 4  # supersampling
    for py in range(s):
        row = bytearray()
        for px in range(s):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    x = px + (sx + 0.5) / ss
                    y = py + (sy + 0.5) / ss
                    if not _inside_rounded(x, y, s, s * 0.22):
                        continue
                    col = BG
                    if _inside_triangle(x, y, s):
                        col = FG
                    elif 0.74 * s <= x <= 0.80 * s and 0.36 * s <= y <= 0.64 * s:
                        col = ACCENT
                    elif 0.84 * s <= x <= 0.90 * s and 0.42 * s <= y <= 0.58 * s:
                        col = ACCENT
                    acc[0] += col[0]; acc[1] += col[1]; acc[2] += col[2]; acc[3] += 255
            n = ss * ss
            a = acc[3] // n
            if a:
                row += bytes((acc[0] * 255 // acc[3], acc[1] * 255 // acc[3], acc[2] * 255 // acc[3], a))
            else:
                row += b"\0\0\0\0"
        rows.append(b"\0" + bytes(row))
    return b"".join(rows)


def png(s: int) -> bytes:
    raw = render(s)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", s, s, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico(pngs: list[tuple[int, bytes]]) -> bytes:
    head = struct.pack("<HHH", 0, 1, len(pngs))
    entries = b""
    data = b""
    offset = 6 + 16 * len(pngs)
    for s, blob in pngs:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(blob), offset)
        data += blob
        offset += len(blob)
    return head + entries + data


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "assets"
    out.mkdir(exist_ok=True)
    big = png(SIZE)
    (out / "icon.png").write_bytes(big)
    (out / "icon.ico").write_bytes(ico([(256, big), (64, png(64)), (32, png(32)), (16, png(16))]))
    print("wrote", out / "icon.png", "and icon.ico")
