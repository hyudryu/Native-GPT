#!/usr/bin/env python3
"""Generate the placeholder Tauri icon (icons/icon.png).

Writes a plain 256x256 PNG (no third-party deps) so `tauri-build` has an
icon to reference. Replace with real artwork later via `tauri icon`.
"""

import struct
import zlib
from pathlib import Path

SIZE = 256
# Dark slate background with a lighter diagonal accent.
def pixel(x: int, y: int) -> bytes:
    if abs(x - y) < 24:
        return bytes((94, 234, 212, 255))  # teal accent
    return bytes((15, 23, 42, 255))  # slate-900


def png(width: int, height: int) -> bytes:
    raw = b"".join(
        b"\x00" + b"".join(pixel(x, y) for x in range(width)) for y in range(height)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


out = Path(__file__).parent / "icon.png"
data = png(SIZE, SIZE)
out.write_bytes(data)
print(f"wrote {out} ({out.stat().st_size} bytes)")

# ICO wrapper (PNG-compressed image, valid for Windows resources).
ico = (
    struct.pack("<HHH", 0, 1, 1)
    + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 6 + 16)
    + data
)
ico_out = Path(__file__).parent / "icon.ico"
ico_out.write_bytes(ico)
print(f"wrote {ico_out} ({ico_out.stat().st_size} bytes)")
