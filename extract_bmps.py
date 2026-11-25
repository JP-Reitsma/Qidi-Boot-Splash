import os
import struct

INPUT = "p5.img"
OUTDIR = "output"

os.makedirs(OUTDIR, exist_ok=True)

with open(INPUT, "rb") as f:
    data = f.read()

count = 0

for i in range(len(data) - 54):
    if data[i:i+2] != b"BM":
        continue

    try:
        filesize = struct.unpack_from("<I", data, i+2)[0]
        pixel_offset = struct.unpack_from("<I", data, i+10)[0]
        dib_size = struct.unpack_from("<I", data, i+14)[0]
        width = struct.unpack_from("<i", data, i+18)[0]
        height = struct.unpack_from("<i", data, i+22)[0]
        planes = struct.unpack_from("<H", data, i+26)[0]
        bpp = struct.unpack_from("<H", data, i+28)[0]
        compression = struct.unpack_from("<I", data, i+30)[0]

        if dib_size != 40:
            continue

        if planes != 1:
            continue

        if abs(width) > 5000 or width == 0:
            continue

        if abs(height) > 5000 or height == 0:
            continue

        if filesize < 100:
            continue

        if i + filesize > len(data):
            continue

        bmp = data[i:i+filesize]

        name = f"bmp_{count:03d}_offset_{i:08X}.bmp"

        with open(os.path.join(OUTDIR, name), "wb") as out:
            out.write(bmp)

        print(
            f"{count:02d}  "
            f"0x{i:08X}  "
            f"{filesize:7d} bytes  "
            f"{width}x{height}  "
            f"{bpp} bpp  "
            f"comp={compression}"
        )

        count += 1

    except Exception:
        pass

print()
print(f"Extracted {count} BMP files.")
