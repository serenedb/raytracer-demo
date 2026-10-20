#!/usr/bin/env python3
"""png.py WIDTH HEIGHT OUT.png < pixels.tsv: rows of `pixel r g b` to a PNG (stdlib only)."""
import struct
import sys
import zlib


def main():
    w, h, out = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    img = bytearray(w * h * 3)
    for line in sys.stdin:
        parts = line.split()
        if len(parts) != 4:
            continue
        p, r, g, b = map(int, parts)
        if 0 <= p < w * h:
            img[p * 3:p * 3 + 3] = bytes((r, g, b))
    raw = b"".join(b"\0" + bytes(img[y * w * 3:(y + 1) * w * 3]) for y in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    with open(out, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(chunk(b"IEND", b""))


if __name__ == "__main__":
    main()
