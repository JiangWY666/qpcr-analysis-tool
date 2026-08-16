"""把圆角图标的黑色画布抠成透明，并生成带 alpha 的多尺寸 ico。"""

from __future__ import annotations

import struct
from collections import deque
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "app_icon.png"
DST_PNG = ROOT / "app_icon.png"
DST_ICO = ROOT / "app.ico"
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _is_black_canvas(pixel: tuple[int, int, int, int]) -> bool:
    red, green, blue, alpha = pixel
    if alpha == 0:
        return True
    # 图标主体是高饱和蓝，暗角和抗锯齿黑边才是画布
    if blue >= 70 and blue > red + 20 and blue > green:
        return False
    return red <= 40 and green <= 50 and blue <= 55


def knock_out_black_canvas(image: Image.Image) -> Image.Image:
    image = image.convert("RGBA")
    pixels = image.load()
    width, height = image.size
    seen = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque(
        ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
    )
    while queue:
        x, y = queue.popleft()
        index = y * width + x
        if seen[index]:
            continue
        seen[index] = 1
        if not _is_black_canvas(pixels[x, y]):
            continue
        pixels[x, y] = (0, 0, 0, 0)
        if x:
            queue.append((x - 1, y))
        if x + 1 < width:
            queue.append((x + 1, y))
        if y:
            queue.append((x, y - 1))
        if y + 1 < height:
            queue.append((x, y + 1))
    return image


def save_png_ico(image: Image.Image, path: Path, sizes: list[tuple[int, int]]) -> None:
    """每个尺寸都用 PNG 写入 ico，保留圆角透明，避免 BMP 把透明变成黑块。"""
    frames: list[tuple[int, int, bytes]] = []
    for width, height in sizes:
        frame = image.resize((width, height), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        frame.save(buffer, format="PNG")
        frames.append((width, height, buffer.getvalue()))

    offset = 6 + 16 * len(frames)
    entries = bytearray()
    blobs = bytearray()
    for width, height, png in frames:
        entries += struct.pack(
            "<BBBBHHII",
            0 if width >= 256 else width,
            0 if height >= 256 else height,
            0,
            0,
            1,
            32,
            len(png),
            offset,
        )
        blobs += png
        offset += len(png)

    path.write_bytes(struct.pack("<HHH", 0, 1, len(frames)) + bytes(entries) + bytes(blobs))


def main() -> None:
    image = knock_out_black_canvas(Image.open(SRC))
    image.save(DST_PNG)
    save_png_ico(image, DST_ICO, SIZES)
    sample = image.getpixel((0, 0))
    print(f"corner alpha={sample[3]}  wrote {DST_PNG.name} and {DST_ICO.name}")


if __name__ == "__main__":
    main()
