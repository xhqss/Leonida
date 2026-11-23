"""Per-channel counts for a mask texture: which channels carry content and what shape it has.

    python -I tools/mask_channels.py <texture.dds> [--thresholds 64,128,192] [--json FILE]

The DDS is decoded with ``dds_png.decode_dds`` (stored values, no sRGB transfer). For each of R, G, B and A the
tool prints the minimum, maximum, mean and number of distinct values, and for each threshold the share of texels
at or above it, the number of 4-connected blobs of such texels and the share of them in the largest blob. A
constant channel carries nothing a selector could pick; a channel of a few large blobs is a broad layer and one of
tens of thousands of small blobs a spotty one. ``docs/gamedata.md`` uses these counts to tie the `AlphaChannels`
`Channel` numbers to texture channels.

Only counts are written. The input is the user's own export and stays local.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import dds_png  # noqa: E402

CHANNELS = "RGBA"


def blobs(mask: bytearray, width: int, height: int) -> list[int]:
    """Sizes of the 4-connected blobs of set texels in a row-major mask, largest first."""
    seen = bytearray(len(mask))
    sizes = []
    for start in range(len(mask)):
        if not mask[start] or seen[start]:
            continue
        seen[start] = 1
        stack, n = [start], 0
        while stack:
            i = stack.pop()
            n += 1
            x = i % width
            for j in (i - 1 if x else -1, i + 1 if x < width - 1 else -1, i - width, i + width):
                if 0 <= j < len(mask) and mask[j] and not seen[j]:
                    seen[j] = 1
                    stack.append(j)
        sizes.append(n)
    return sorted(sizes, reverse=True)


def channel_counts(width: int, height: int, rgba: bytes, thresholds=(64, 128, 192)) -> dict:
    """Counts for each channel of an RGBA8 image (see the module docstring)."""
    texels = width * height
    out = {}
    for c, name in enumerate(CHANNELS):
        values = rgba[c::4]
        row = {"min": min(values), "max": max(values), "mean": round(sum(values) / texels, 2),
               "distinct": len(set(values)), "thresholds": {}}
        for t in thresholds:
            mask = bytearray(v >= t for v in values)
            covered = sum(mask)
            sizes = blobs(mask, width, height) if row["min"] != row["max"] else ([covered] if covered else [])
            row["thresholds"][t] = {"cover": round(covered / texels, 4), "blobs": len(sizes),
                                    "largest_share": round(sizes[0] / covered, 4) if covered else 0.0}
        out[name] = row
    return out


def same_file(first: str, second: str) -> bool:
    """True when opening ``second`` for writing would truncate ``first`` (same path, symlink or hard link)."""
    if os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second)):
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dds", help="an exported .dds texture")
    ap.add_argument("--thresholds", default="64,128,192", help="comma-separated 0..255 levels (default 64,128,192)")
    ap.add_argument("--json", help="also write the counts here")
    a = ap.parse_args(argv)
    try:
        thresholds = tuple(int(t) for t in a.thresholds.split(","))
    except ValueError:
        ap.error("--thresholds takes integers")
    if not thresholds or any(not 0 < t <= 255 for t in thresholds):
        ap.error("--thresholds must be within 1..255")
    if a.json and same_file(a.dds, a.json):
        ap.error("--json names the input texture; writing it would replace the DDS")
    with open(a.dds, "rb") as fh:
        width, height, rgba = dds_png.decode_dds(fh.read())
    counts = channel_counts(width, height, rgba, thresholds)
    print(f"{width}x{height}")
    for name, row in counts.items():
        parts = [f"{t}: cover {v['cover']:.3f} blobs {v['blobs']} largest {v['largest_share']:.3f}"
                 for t, v in row["thresholds"].items()]
        print(f"{name} min {row['min']} max {row['max']} mean {row['mean']} distinct {row['distinct']} | "
              + " | ".join(parts))
    if a.json:
        with open(a.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"width": width, "height": height, "channels": counts}, fh, indent=2)
    return 0


if __name__ == "__main__":
    try:  # a redirected stdout is strict ANSI code page under -I; write '?' for what it cannot encode
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    sys.exit(main())
