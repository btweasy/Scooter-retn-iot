"""Generuje ikony PNG aplikacji (PWA) z tego samego rysunku co backend/web/icons/icon.svg.

Bez Pillow: kształty liczone jako odległość od linii/okręgu (numpy), wygładzanie krawędzi,
zapis PNG ręcznie (zlib). Uruchomienie: py tools/make_icons.py
"""
import struct
import zlib
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent.parent / "backend" / "web" / "icons"
BACKGROUND = (0x0E, 0x1B, 0x1F)
LIME = (0xB5, 0xF2, 0x3D)

# rysunek w układzie 512 x 512 (jak w icon.svg)
WHEELS = [((150, 360), 50, 15), ((366, 360), 50, 15)]           # środek, promień, pół grubości
LINES = [((150, 360), (190, 300)), ((190, 300), (356, 300)),((366, 360), (330, 136)), ((294, 136), (366, 136))]
LINE_HALF_WIDTH = 17


def distance_to_segment(x, y, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = np.clip(((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy), 0, 1)
    return np.hypot(x - (ax + t * dx), y - (ay + t * dy))


def coverage(distance, pixel):
    """0..1 – jak bardzo piksel jest wewnątrz kształtu (miękka krawędź szerokości 1 piksela)."""
    return np.clip(0.5 - distance / pixel, 0, 1)


def render(size: int, rounded: bool, glyph_scale: float) -> np.ndarray:
    pixel = 512 / size
    coords = (np.arange(size) + 0.5) * pixel
    x, y = np.meshgrid(coords, coords)

    # tło: zaokrąglony kwadrat albo pełny (ikona "maskable" – system sam przycina kształt)
    if rounded:
        radius = 112
        qx = np.maximum(np.abs(x - 256) - (256 - radius), 0)
        qy = np.maximum(np.abs(y - 256) - (256 - radius), 0)
        background = coverage(np.hypot(qx, qy) - radius, pixel)
    else:
        background = np.ones_like(x)

    # rysunek hulajnogi, opcjonalnie pomniejszony wokół środka
    gx = 256 + (x - 256) / glyph_scale
    gy = 256 + (y - 256) / glyph_scale
    glyph_pixel = pixel / glyph_scale
    glyph = np.zeros_like(x)
    for center, radius, half in WHEELS:
        ring = np.abs(np.hypot(gx - center[0], gy - center[1]) - radius) - half
        glyph = np.maximum(glyph, coverage(ring, glyph_pixel))
    for a, b in LINES:
        glyph = np.maximum(glyph, coverage(distance_to_segment(gx, gy, a, b) - LINE_HALF_WIDTH, glyph_pixel))

    image = np.zeros((size, size, 4))
    for channel in range(3):
        image[..., channel] = BACKGROUND[channel] * (1 - glyph) + LIME[channel] * glyph
    image[..., 3] = background * 255
    return image.round().astype(np.uint8)


def save_png(path: Path, image: np.ndarray):
    height, width, _ = image.shape
    raw = b"".join(b"\x00" + image[row].tobytes() for row in range(height))   # filtr 0 w każdym wierszu

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)   # 8 bitów, RGBA
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    print("zapisano", path)


if __name__ == "__main__":
    save_png(OUT / "icon-192.png", render(192, rounded=True, glyph_scale=1))
    save_png(OUT / "icon-512.png", render(512, rounded=True, glyph_scale=1))
    save_png(OUT / "icon-maskable-512.png", render(512, rounded=False, glyph_scale=0.78))
    save_png(OUT / "apple-touch-icon.png", render(180, rounded=False, glyph_scale=0.85))
