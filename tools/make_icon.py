"""Draws the Wipe Review icon - a magnifying glass over a skull with red eye
sockets (reviewing the wipe) on the app's blurple - and writes
assets/icon.png (256px) and assets/icon.ico (16-256px).

Pure standard library: the shapes are rasterised here with supersampling for
anti-aliasing, and each size is drawn from the vector shapes, not scaled.

    python tools/make_icon.py
"""

import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZES = [16, 24, 32, 48, 64, 128, 256]

BLURPLE = (88, 101, 242)
WHITE = (242, 243, 245)
DARK = (30, 31, 34)
RED = (242, 63, 67)


# --- shapes, in 0..1 coordinates ----------------------------------------------
def circle(cx, cy, r):
    return lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def rounded_rect(x0, y0, x1, y1, r):
    def inside(x, y):
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        dx = max(x0 + r - x, 0, x - (x1 - r))
        dy = max(y0 + r - y, 0, y - (y1 - r))
        return dx * dx + dy * dy <= r * r
    return inside


def capsule(ax, ay, bx, by, r):
    """A thick line with round ends."""
    vx, vy = bx - ax, by - ay
    length2 = vx * vx + vy * vy

    def inside(x, y):
        t = max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / length2))
        px, py = ax + t * vx - x, ay + t * vy - y
        return px * px + py * py <= r * r
    return inside


def triangle(p1, p2, p3):
    def sign(a, b, c):
        return (a[0] - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (a[1] - c[1])

    def inside(x, y):
        p = (x, y)
        d1, d2, d3 = sign(p, p1, p2), sign(p, p2, p3), sign(p, p3, p1)
        return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))
    return inside


# Painted bottom to top; the first layer that contains a sample (searching
# from the top) gives its colour. Lens centre (0.42, 0.42).
LAYERS = [
    (rounded_rect(0.03, 0.03, 0.97, 0.97, 0.22), BLURPLE),
    (capsule(0.62, 0.62, 0.85, 0.85, 0.075), WHITE),           # handle
    (circle(0.42, 0.42, 0.305), WHITE),                         # lens rim
    (circle(0.42, 0.42, 0.24), DARK),                           # lens
    (circle(0.42, 0.395, 0.14), WHITE),                         # cranium
    (rounded_rect(0.345, 0.43, 0.495, 0.545, 0.03), WHITE),     # jaw
    (circle(0.37, 0.405, 0.042), RED),                          # eyes
    (circle(0.47, 0.405, 0.042), RED),
    (triangle((0.42, 0.445), (0.402, 0.48), (0.438, 0.48)), DARK),  # nose
    (rounded_rect(0.389, 0.505, 0.401, 0.548, 0.004), DARK),    # teeth gaps
    (rounded_rect(0.439, 0.505, 0.451, 0.548, 0.004), DARK),
]


def render(size, samples=4):
    """RGBA bytes for a size x size image, samples^2 samples per pixel."""
    rows = []
    step = 1.0 / (size * samples)
    offsets = [(i + 0.5) * step for i in range(samples)]
    n = samples * samples
    for py in range(size):
        row = bytearray([0])  # PNG filter type 0
        for px in range(size):
            r = g = b = a = 0
            for oy in offsets:
                y = py / size + oy
                for ox in offsets:
                    x = px / size + ox
                    for shape, color in reversed(LAYERS):
                        if shape(x, y):
                            r += color[0]; g += color[1]; b += color[2]; a += 1
                            break
            if a:
                row += bytes((r // a, g // a, b // a, round(255 * a / n)))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return b"".join(rows)


def png(size, rgba_rows):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rgba_rows, 9)) + chunk(b"IEND", b"")


def ico(pngs):
    """An .ico holding PNG-compressed images (supported since Windows Vista)."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b"", b""
    for size, data in pngs:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    return header + entries + blobs


def main():
    ASSETS.mkdir(exist_ok=True)
    images = []
    for size in SIZES:
        data = png(size, render(size, samples=4 if size >= 64 else 6))
        images.append((size, data))
        print(f"  {size}px")
    (ASSETS / "icon.png").write_bytes(images[-1][1])
    (ASSETS / "icon.ico").write_bytes(ico(images))
    print(f"wrote {ASSETS / 'icon.png'} and {ASSETS / 'icon.ico'}")


if __name__ == "__main__":
    main()
