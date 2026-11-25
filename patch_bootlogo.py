from __future__ import annotations

import argparse
import struct
from pathlib import Path

from PIL import Image

TARGET_WIDTH = 480
TARGET_HEIGHT = 272
LOGO_OFFSETS = (0x00647000, 0x006A6C00)


def fit_image(source: Path) -> Image.Image:
    """Crop and resize artwork to the panel's native bitmap dimensions."""
    target_size = (TARGET_WIDTH, TARGET_HEIGHT)

    image = Image.open(source).convert("RGB")
    target_ratio = target_size[0] / target_size[1]
    source_ratio = image.width / image.height

    if source_ratio > target_ratio:
        width = round(image.height * target_ratio)
        left = (image.width - width) // 2
        image = image.crop((left, 0, left + width, image.height))
    else:
        height = round(image.width / target_ratio)
        top = (image.height - height) // 2
        image = image.crop((0, top, image.width, top + height))

    image = image.resize(target_size, Image.Resampling.LANCZOS)
    return image


def patch_bmp(data: bytearray, offset: int, image: Image.Image) -> None:
    if data[offset:offset + 2] != b"BM":
        raise ValueError(f"No BMP found at offset 0x{offset:X}")

    file_size = struct.unpack_from("<I", data, offset + 2)[0]
    pixel_offset = struct.unpack_from("<I", data, offset + 10)[0]
    dib_size = struct.unpack_from("<I", data, offset + 14)[0]
    width = struct.unpack_from("<i", data, offset + 18)[0]
    height = struct.unpack_from("<i", data, offset + 22)[0]
    planes = struct.unpack_from("<H", data, offset + 26)[0]
    bits_per_pixel = struct.unpack_from("<H", data, offset + 28)[0]
    compression = struct.unpack_from("<I", data, offset + 30)[0]

    if (dib_size, width, abs(height), planes, bits_per_pixel, compression) != (40, 480, 272, 1, 24, 0):
        raise ValueError(
            f"Unexpected BMP at 0x{offset:X}: {width}x{height}, {bits_per_pixel}bpp, "
            f"DIB={dib_size}, planes={planes}, compression={compression}"
        )

    row_stride = ((width * 3 + 3) // 4) * 4
    rows = range(TARGET_HEIGHT) if height < 0 else range(TARGET_HEIGHT - 1, -1, -1)
    pixels = bytearray()
    for y in rows:
        row = bytearray()
        for x in range(TARGET_WIDTH):
            red, green, blue = image.getpixel((x, y))
            row.extend((blue, green, red))
        row.extend(b"\0" * (row_stride - len(row)))
        pixels.extend(row)

    if len(pixels) != file_size - pixel_offset:
        raise ValueError(f"Pixel data size mismatch at 0x{offset:X}")

    start = offset + pixel_offset
    data[start:start + len(pixels)] = pixels


def main() -> None:
    parser = argparse.ArgumentParser(description="Replace the two 480x272 QIDI boot logos in a firmware image.")
    parser.add_argument("firmware", type=Path, help="Your own compatible p5.img firmware image")
    parser.add_argument("artwork", type=Path, nargs="?", help="One PNG/JPEG used for both boot moments")
    parser.add_argument("--startup-artwork", type=Path, help="Artwork for the brief startup splash at 0x647000")
    parser.add_argument("--main-artwork", type=Path, help="Artwork for the main boot splash at 0x6A6C00")
    parser.add_argument("-o", "--output", type=Path, default=Path("p5_patched.img"))
    args = parser.parse_args()

    if not args.firmware.is_file():
        parser.error(f"Firmware image not found: {args.firmware}")
    if args.artwork and (args.startup_artwork or args.main_artwork):
        parser.error("Use either one positional artwork file or --startup-artwork/--main-artwork")
    if args.artwork:
        startup_path = main_path = args.artwork
    elif args.startup_artwork and args.main_artwork:
        startup_path, main_path = args.startup_artwork, args.main_artwork
    else:
        parser.error("Provide artwork or both --startup-artwork and --main-artwork")
    for artwork_path in {startup_path, main_path}:
        if not artwork_path.is_file():
            parser.error(f"Artwork file not found: {artwork_path}")

    if args.output.resolve() == args.firmware.resolve():
        parser.error("Output must be a different file from the input firmware")

    images = {LOGO_OFFSETS[0]: fit_image(startup_path), LOGO_OFFSETS[1]: fit_image(main_path)}
    data = bytearray(args.firmware.read_bytes())
    for offset in LOGO_OFFSETS:
        patch_bmp(data, offset, images[offset])
        print(f"Patched boot logo at 0x{offset:08X}")

    args.output.write_bytes(data)
    print(f"Wrote {args.output} ({len(data):,} bytes)")


if __name__ == "__main__":
    main()


