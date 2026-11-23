"""Decode the clip dictionaries in anim.rpf: clips, animations and their keyframe channels.

The table of contents is encrypted, so this starts from the same stream walk as anim_index.py: every zlib stream
that inflates cleanly to its end. A clip dictionary is a RAGE resource whose system pages are one or two of those
streams. Its root stream starts with 8 zero bytes, a 0x50xxxxxx page-map pointer at +8 and zeros up to +0x38. The
page map's byte at +8 gives the number of system pages, and the dictionary takes that many back-to-back streams.
A last page that anim_index does not list (a zlib stream inflating to fewer than 64 bytes, or a 16-byte raw page
right after the stream) is joined when the pages then parse. Pointers are then offsets into the joined pages
(pointer - 0x50000000). See docs/animation.md, "Keyframe decoding".

Hash-only references are joined across the archive: a root pack reference with a null pointer is another
dictionary's joaat32(name), and each `.motion` child is {clip hash, dictionary hash}; the counts say how many of
them are found in the same archive.

Every animation is decoded down to the per-frame values. The default output is counts only: the number of
dictionaries, clips, animations, sequences, channels by type and track types. The checks are also counted: channel
definitions ending at the frame data, type-10 chunk closure, sequence boundaries and quaternion norms. The local
JSON adds one row per dictionary (offset, hash, counts, and its name, read from your own copy). No values or
payloads are written.

Usage:
    python -I anim_clips.py <anim.rpf> <out.json> [--start N] [--limit BYTES] [--jobs N] [--quiet]
    python -I anim_clips.py <anim.rpf> --decode <dictionary offset> [--frames N]

`--decode` prints one dictionary's clips and the decoded track values (first N frames, default 4) as JSON on
stdout, for local inspection. It writes no file.
"""
import argparse
import collections
import json
import math
import mmap
import os
import struct
import sys
import time
import zlib

# -I keeps the script directory off sys.path, so add it explicitly before importing the stream walk.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anim_index  # noqa: E402
from pool_workers import process_workers  # noqa: E402

SYSTEM = 0x50000000
CHANNEL_NAMES = ("static_quat", "static_vec3", "static_float", "raw_float", "quantize_float",
                 "indirect_quantize_float", "linear_float", "cached_quat", "quat_normalize", "type9",
                 "delta2_float")
# Track-type hashes are joaat32 of these names.
TRACK_TYPE_NAMES = ("bonerotation", "bonetranslation", "bonescale", "genericcontrol", "generictranslation",
                    "facialtranslation", "cameratranslation", "camerarotation")
TRACK_TYPES = {anim_index.joaat32(n): n for n in TRACK_TYPE_NAMES}
TRACK_FORMATS = {0: "quaternion", 2: "vector3", 4: "float"}

# `.motion` axis names: joaat32 of the lower-case name, recovered by brute force over a word list
AXIS_NAMES = {anim_index.joaat32(n): n for n in ("yawangle", "rotationz", "facingdirection", "speed", "surfaceincline",
                                                "roll", "verticalheight", "pitch", "pitchangle", "height")}


class DecodeError(ValueError):
    pass


class NotClipDictionary(DecodeError):
    """A root with the dictionary header whose +0x70 is not a pack array."""


class System:
    """Joined system pages of one resource. Non-null pointers must land inside the pages."""

    def __init__(self, data):
        self.d = data

    def u16(self, at):
        return struct.unpack_from("<H", self.d, at)[0]

    def u32(self, at):
        return struct.unpack_from("<I", self.d, at)[0]

    def u64(self, at):
        return struct.unpack_from("<Q", self.d, at)[0]

    def f32(self, at):
        return struct.unpack_from("<f", self.d, at)[0]

    def ptr(self, at):
        if at < 0 or at + 8 > len(self.d):
            raise DecodeError(f"field at 0x{at:x} is outside the pages")
        q = self.u64(at)
        if q == 0:
            return None
        if not SYSTEM <= q < SYSTEM + len(self.d):
            raise DecodeError(f"pointer 0x{q:x} at 0x{at:x} is outside the pages")
        return q - SYSTEM

    def string(self, at):
        """16-byte atString: {char *ptr; u16 length; u16 capacity}, or a short string stored inline, in which
        case byte 15 is 0x80 | length and the characters start at byte 0."""
        if at < 0 or at + 16 > len(self.d):
            raise DecodeError(f"string at 0x{at:x} is outside the pages")
        tag = self.d[at + 15]
        if tag & 0x80:
            n = tag & 0x7F
            if n > 15:
                raise DecodeError(f"inline string at 0x{at:x} is {n} bytes")
            return self.d[at:at + n].decode("latin-1")
        p = self.ptr(at)
        if p is None:
            return None
        n = self.u16(at + 8)
        if p + n > len(self.d):
            raise DecodeError("string runs past the pages")
        return self.d[p:p + n].decode("latin-1")

    def array(self, at):
        """{T *ptr; u16 count; u16 capacity} -> (offset or None, count)"""
        p = self.ptr(at)
        return (p, self.u16(at + 8)) if p is not None else (None, 0)

    def atmap(self, at):
        """atMap {Entry **buckets; u32 bucket count; ...}, Entry {u64 key; T *value; Entry *next}"""
        buckets, n = self.ptr(at), self.u32(at + 8)
        if buckets is None:
            return []
        if n > 4096:
            raise DecodeError(f"atMap at 0x{at:x} has {n} buckets")
        out, seen = [], set()
        for i in range(n):
            e = self.ptr(buckets + 8 * i)
            while e is not None:
                if e in seen:
                    raise DecodeError("atMap chain loops")
                seen.add(e)
                out.append((self.u64(e), self.ptr(e + 8)))
                e = self.ptr(e + 16)
        return out


# ---------------------------------------------------------------- structure


def parse_animation(s, at):
    seqs, nseq = s.array(at + 0x50)
    tracks, ntr = s.array(at + 0x60)
    return {
        "offset": at, "frames": s.u16(at + 0x24), "duration": s.f32(at + 0x28),
        "sequences": [s.ptr(seqs + 8 * i) for i in range(nseq)],
        "tracks": [struct.unpack_from("<3I", s.d, tracks + 12 * i) for i in range(ntr)],
    }


def motion_times(s, at):
    """`.motion` +0x70: one 0x28-byte entry per child, {f32 child duration, f32 duration in the blend,
    {ptr, u16 count} curve of (child time, blend time) f32 pairs, 16 zero bytes}. -> [(duration, blend, points)]"""
    p, n = s.array(at + 0x70)
    out = []
    for i in range(n if p is not None else 0):
        e = p + 0x28 * i
        cp, cn = s.array(e + 8)
        points = [(s.f32(cp + 8 * j), s.f32(cp + 8 * j + 4)) for j in range(cn)] if cp is not None else []
        out.append((s.f32(e), s.f32(e + 4), points))
    return out


# Where a multi-axis node keeps its sample count (the number of +0x90 sample lists of a one-node `.motion`)
NODE_SAMPLE_COUNT_AT = {4: 0x90, 5: 0x90, 6: 0x50, 8: 0xA0}


def parameter_node(s, at, parameters):
    """The node that places a `.motion`'s children in parameter space: the pointer at +0x138 when +0x58 is 1;
    when +0x58 is 2, +0x120 points at a block whose +0x18 is the node. A node starts {u64 0, u32 type, u32 0}.
    Types 3, 4, 5, 6 and 8 have 1, 2, 3, 4 and 5 axes: +0x10 is an array of 8-byte {u32 name hash, u16, u16}.
    Type 3 is one sampled axis: then an array of (u32 sample index, f32 value) sorted by value, f32 min, f32 max.
    Types 4, 5, 6 and 8 keep their sample count at NODE_SAMPLE_COUNT_AT, and +0x20 is a {ptr, count} array of
    16-byte items ("pairs": words 0 and 2; {u32, u32 0, u32, u32 0} on types 6 and 8, null on type 4, not decoded);
    the rest of them is not decoded.
    Type 1 has no axes and its sample count (1) at +0x20.
    -> (type, axis or None) or None; axis = {names, slots, sample_count, and samples, min, max for type 3 or
    pairs for types 4-8}"""
    if parameters == 1:
        node = s.ptr(at + 0x138)
    elif parameters == 2:
        block = s.ptr(at + 0x120)
        node = s.ptr(block + 0x18) if block is not None else None
    else:
        return None
    if node is None or s.u64(node) != 0 or s.u32(node + 12) != 0:
        return None
    kind, axis = s.u32(node + 8), None
    if kind == 1:
        axis = {"names": [], "slots": [], "sample_count": s.u32(node + 0x20)}
    elif kind == 3 or kind in NODE_SAMPLE_COUNT_AT:
        names, nn = s.array(node + 0x10)
        if names is not None:
            axis = {"names": [s.u32(names + 8 * i) for i in range(nn)],
                    "slots": [(s.u16(names + 8 * i + 4), s.u16(names + 8 * i + 6)) for i in range(nn)]}
            if kind == 3:
                samples, ns = s.array(node + 0x20)
                if samples is None:
                    return kind, None
                axis.update(samples=[(s.u32(samples + 8 * i), s.f32(samples + 8 * i + 4)) for i in range(ns)],
                            min=s.f32(node + 0x30), max=s.f32(node + 0x34), sample_count=ns)
            else:
                axis["sample_count"] = s.u32(node + NODE_SAMPLE_COUNT_AT[kind])
                pairs, npairs = s.array(node + 0x20)     # null on type 4 and most type 5
                axis["pairs"] = None if pairs is None else [(s.u32(pairs + 16 * i), s.u32(pairs + 16 * i + 8))
                                                            for i in range(npairs)]
    return kind, axis


def motion_samples(s, at):
    """`.motion` +0x90: one {ptr, u16 count} list per sample of the parameter node, each item a 12-byte
    {u32 child index, f32, f32}. A one-node `.motion` has as many lists as its node has samples, so type-3 sample
    index k plays child list[k][0]. -> [[(child, f32, f32)]] ([] when +0x90 is null)"""
    p, n = s.array(at + 0x90)
    out = []
    for i in range(n if p is not None else 0):
        items, count = s.array(p + 16 * i)
        out.append([(s.u32(items + 12 * j), s.f32(items + 12 * j + 4), s.f32(items + 12 * j + 8))
                    for j in range(count)] if items is not None else [])
    return out


def parse_clip(s, at):
    kind = s.u32(at + 0x20)
    if kind >= 16:  # +0x20 is a pointer: a parametric clip that refers to packs outside the dictionary
        name_at, tag = s.u64(at + 0x30), s.d[at + 0x3F]
        # `.motion`: name at +0x30, a pointer or inline (+0x3F 0x80 | length). For a `.motionmatcher` +0x3F is
        # byte 7 of its name at +0x38: a pointer's zero high byte or an inline character.
        if SYSTEM <= name_at < SYSTEM + len(s.d) or tag & 0x80 and tag & 0x7F <= 15:
            motion = {"kind": "motion", "name": s.string(at + 0x30), "animations": [], "children": None,
                      "parameters": None}
            try:
                # +0x58 u32 parameter count; +0x60 array of {u64 0; u32 clip hash; u32 dictionary hash}
                children, n = s.array(at + 0x60)
                if children is None:
                    motion["children"], motion["parameters"] = [], s.u32(at + 0x58)
                elif children + 16 * n <= len(s.d) and all(s.u64(children + 16 * i) == 0 for i in range(n)):
                    motion["children"] = [(s.u32(children + 16 * i + 8), s.u32(children + 16 * i + 12))
                                          for i in range(n)]
                    motion["parameters"] = s.u32(at + 0x58)
            except (DecodeError, struct.error):
                pass
            try:  # read apart: a malformed node must not mark a readable time table unreadable
                motion["times"] = motion_times(s, at)
            except (DecodeError, struct.error):
                motion["times"] = None
            try:
                motion["node"] = parameter_node(s, at, motion["parameters"])
            except (DecodeError, struct.error):
                motion["node"] = None
            try:
                motion["samples"] = motion_samples(s, at)
            except (DecodeError, struct.error):
                motion["samples"] = None
            return motion
        # `.motionmatcher`: a 16-byte id at +0x28, name at +0x38
        return {"kind": "motionmatcher", "name": s.string(at + 0x38), "animations": []}
    clip = {"kind": "clip", "name": s.string(at + 0x28), "duration": s.f32(at + 0x78), "animations": []}
    lst = s.ptr(at + 0x68)
    if lst is not None:
        entries, n = s.array(lst)
        for i in range(n):
            e = entries + 0x38 * i
            clip["animations"].append({"animation": s.ptr(e), "hash": s.u32(e + 8), "start": s.f32(e + 0x28),
                                       "end": s.f32(e + 0x2C), "rate": s.f32(e + 0x30)})
    return clip


def parse_dictionary(s):
    """Root object at page offset 0: +0x38 name, +0x48 clip map, +0x68 joaat32(name), +0x70 pack references."""
    refs_word = s.u64(0x70)
    if refs_word and not SYSTEM <= refs_word < SYSTEM + len(s.d):
        # Same root layout, other content: "RIG" + version at +0x70 is a rig dictionary.
        raise NotClipDictionary("rig" if s.d[0x70:0x73] == b"RIG" else "other")
    d = {"name": s.string(0x38), "hash": s.u32(0x68), "clips": [], "packs": [], "external_packs": 0,
         "external_hashes": []}
    for key, value in s.atmap(0x48):
        if value is not None:
            clip = parse_clip(s, value)
            clip["hash"] = key & 0xFFFFFFFF          # the key is joaat32(dictionary) << 32 | clip hash
            d["clips"].append(clip)
    refs, n = s.array(0x70)
    for i in range(n):
        pack = s.ptr(refs + 16 * i)
        if pack is None:
            d["external_packs"] += 1        # only a hash: another clip dictionary's joaat32(name)
            d["external_hashes"].append(s.u32(refs + 16 * i + 8))
            continue
        anims = [parse_animation(s, v) for _k, v in s.atmap(pack + 0x48) if v is not None]
        d["packs"].append({"name": s.string(pack + 0x38), "hash": s.u32(pack + 0x68), "animations": anims})
    return d


# ---------------------------------------------------------------- channels


def zigzag(u):
    return (u >> 1) if not u & 1 else -((u + 1) >> 1)


def _bits(blob, pos, k):
    if not k:
        return 0
    o = pos >> 3
    v = int.from_bytes(blob[o:o + ((pos & 7) + k + 7 >> 3)], "little")
    return (v >> (pos & 7)) & ((1 << k) - 1)


def decode_delta2(data, at, nf, stats=None):
    """Channel type 10, second-order delta coded. Returns (values, byte length).

    u32 word count | u8 c1, c2, c3, 0 | f32 quantum | f32 offset | LSB-first bits:
    nc = ceil(nf / 64) chunk payload positions (c1 bits each), nc chunk start values (c2 bits),
    ceil((nf - 1) / 8) unary widths, one per 8 deltas (k zeros then a one), and at the end 8 lanes x c3 u32 words,
    interleaved (word w * 8 + j is lane j). Lane j holds delta j of every 8-delta group, k bits each.
    In a chunk: x = start, v = 0; for each frame v += zigzag(raw); x += v. Value = x * quantum + offset.
    """
    words, packed, quantum, offset = struct.unpack_from("<IIff", data, at)
    c1, c2, c3 = packed & 255, (packed >> 8) & 255, (packed >> 16) & 255
    size = 4 * words
    if size < 16 or at + size > len(data):
        raise DecodeError("type-10 channel runs past the sequence")
    blob = bytes(data[at + 16:at + size])
    nbits = 8 * len(blob)
    nc, ng = (nf + 63) // 64, (nf + 6) // 8      # nf - 1 deltas, 8 per width code
    starts = [_bits(blob, nc * c1 + i * c2, c2) for i in range(nc)]
    pos = nc * (c1 + c2)
    widths = []
    for _ in range(ng):                     # unary: k zero bits, then a one
        o, k = pos >> 3, 0
        byte = blob[o] >> (pos & 7) if o < len(blob) else 0
        if byte:
            k = (byte & -byte).bit_length() - 1
        else:
            k = 8 - (pos & 7)
            o += 1
            while o < len(blob) and not blob[o]:
                k += 8
                o += 1
            if o >= len(blob):
                raise DecodeError("type-10 width code runs past the channel")
            k += (blob[o] & -blob[o]).bit_length() - 1
        pos += k + 1
        widths.append(k)
    lane_at = nbits - 256 * c3
    if lane_at < pos:
        raise DecodeError("type-10 lanes overlap the width codes")
    if sum(widths) > 32 * c3:
        raise DecodeError("type-10 widths need more lane bits than the channel has")
    lw = struct.unpack_from(f"<{8 * c3}I", blob, lane_at // 8)
    lanes = [b"".join(struct.pack("<I", lw[w * 8 + j]) for w in range(c3)) for j in range(8)]
    raw, lp = [], 0
    for k in widths:
        for j in range(8):
            raw.append(_bits(lanes[j], lp, k))
        lp += k
    values = [0.0] * nf
    for ci in range(nc):
        x, v, f0 = starts[ci], 0, ci * 64
        values[f0] = x * quantum + offset
        for j in range(64):
            f = f0 + j + 1
            if f >= nf:
                break
            v += zigzag(raw[f0 + j])
            x += v
            if j == 63:
                if stats is not None:
                    stats["delta2_boundaries"] += 1
                    stats["delta2_boundaries_closed"] += x == starts[ci + 1]
                break
            values[f] = x * quantum + offset
    return values, size


def sequence_header(data, at):
    h = struct.unpack_from("<5I5H2B", data, at)
    return {"length": h[1], "frame_offset": h[3], "frames": h[6], "frame_bytes": h[7], "quantize_bits": h[9]}


def decode_sequence(s, at, stats=None):
    """One sequence: 0x20-byte header, then channel definitions, frame bits, u16 counts[11] and the
    (track << 2 | component) lists. Returns (frames, channels) with channels = [type, track, comp, values]."""
    h = sequence_header(s.d, at)
    data = memoryview(s.d)[at + 0x20:at + 0x20 + h["length"]]
    nf, fo, fl = h["frames"], h["frame_offset"], h["frame_bytes"]
    lists_at = fo + fl * nf
    if lists_at + 22 > len(data):
        raise DecodeError("sequence counts run past its data")
    counts = struct.unpack_from("<11H", data, lists_at)
    lists, p = [], lists_at + 22
    for c in counts:
        lists.append([(x >> 2, x & 3) for x in struct.unpack_from(f"<{c}H", data, p)])
        p += 2 * ((c + 3) // 4 * 4)
    p, channels, frame_fields = 0, [], []
    for t, c in enumerate(counts):
        if c and t in (6, 9):
            raise DecodeError(f"channel type {t} ({CHANNEL_NAMES[t]}) is not decoded")
        for i in range(c):
            track, comp = lists[t][i]
            value = None
            if t == 0 or t == 1:
                value = struct.unpack_from("<3f", data, p)
                p += 12
            elif t == 2:
                value = struct.unpack_from("<f", data, p)[0]
                p += 4
            elif t == 3:                      # raw f32 per frame, no definition bytes
                frame_fields.append((len(channels), 32, "raw", None))
            elif t == 4:
                bits, quantum, offset = struct.unpack_from("<Iff", data, p)
                p += 12
                frame_fields.append((len(channels), bits, quantum, offset))
            elif t == 5:
                fbits, vbits, words, quantum, offset = struct.unpack_from("<IIIff", data, p)
                table_bytes = bytes(data[p + 20:p + 20 + 4 * words])
                p += 20 + 4 * words
                n = (32 * words) // vbits if vbits else 1
                table = [(_bits(table_bytes, i * vbits, vbits) if vbits else 0) * quantum + offset for i in range(n)]
                frame_fields.append((len(channels), fbits, "table", table))
            elif t == 10:
                value, size = decode_delta2(data, p, nf, stats)
                p += size
            channels.append([t, track, comp, value])
    if p != fo:
        raise DecodeError(f"channel definitions end at 0x{p:x}, frame data starts at 0x{fo:x}")
    if stats is not None:
        stats["quantize_bits_match"] += sum(b for _i, b, q, _o in frame_fields if q not in ("raw", "table")) \
            == h["quantize_bits"]
    if frame_fields:
        need = sum(b for _i, b, _q, _o in frame_fields)
        if need > 8 * fl:
            raise DecodeError(f"frame fields need {need} bits, frames have {8 * fl}")
        columns = {i: [] for i, *_ in frame_fields}
        for f in range(nf):
            row = int.from_bytes(data[fo + f * fl:fo + (f + 1) * fl], "little")
            for i, bits, quantum, offset in frame_fields:
                x = row & ((1 << bits) - 1)
                row >>= bits
                if quantum == "raw":
                    columns[i].append(struct.unpack("<f", struct.pack("<I", x))[0])
                elif quantum == "table":
                    if x >= len(offset):
                        raise DecodeError("indirect index past its value table")
                    columns[i].append(offset[x])
                else:
                    columns[i].append(x * quantum + offset)
        for i, col in columns.items():
            channels[i][3] = col
    return nf, channels


def assemble_tracks(nf, channels, ntracks, stats=None):
    """Channels -> {track: [per-frame tuple]}. Quaternions are (x, y, z, w).

    Type 7 on a track with three stored components (comp 0..2): rebuild the missing component, placed at the
    type-7 channel's comp position, as +sqrt(1 - sum of squares); the stored three fill the other positions in
    order. Type 8 on a track with all four components stored: use them and normalise (nothing is rebuilt)."""
    by = collections.defaultdict(list)
    for t, track, comp, v in channels:
        if track >= ntracks:
            raise DecodeError("channel names a track past the track list")
        by[track].append((t, comp, v))
    out = {}
    for track, chs in by.items():
        whole = [c for c in chs if c[0] in (0, 1)]
        if whole:
            t, _comp, v = whole[0]
            if t == 0:
                x, y, z = v
                r = 1.0 - x * x - y * y - z * z
                if stats is not None:
                    stats["quaternion_static"] += 1
                    stats["quaternion_static_xyz_over_unit"] += r < -1e-3
                v = (x, y, z, math.sqrt(max(0.0, r)))
            out[track] = [tuple(v)] * nf
            continue
        comps, rebuild, normalise = {}, None, False
        for t, comp, v in chs:
            if t == 7:
                rebuild = comp
            elif t == 8:
                normalise = True
            else:
                comps[comp] = v if isinstance(v, list) else [v] * nf
        if rebuild is not None and sorted(comps) != [0, 1, 2]:
            raise DecodeError("type-7 channel on a track without exactly three stored components")
        if normalise and sorted(comps) != [0, 1, 2, 3]:
            raise DecodeError("type-8 channel on a track without four stored components")
        n = 3 if rebuild is not None else (max(comps) + 1 if comps else 0)
        frames, worst = [], 0.0
        for f in range(nf):
            vec = [comps[c][f] if c in comps else 0.0 for c in range(n)]
            if rebuild is not None:
                r = 1.0 - sum(a * a for a in vec)
                worst = max(worst, -r)
                vec.insert(rebuild, math.sqrt(max(0.0, r)))
            elif n == 4:
                worst = max(worst, abs(math.sqrt(sum(a * a for a in vec)) - 1.0))
            if rebuild is not None or normalise:
                norm = math.sqrt(sum(a * a for a in vec)) or 1.0
                vec = [a / norm for a in vec]      # quantisation leaves |q| slightly off 1
            frames.append(tuple(vec))
        if stats is not None and (rebuild is not None or n == 4):
            kind = "rebuilt" if rebuild is not None else ("type8_four_stored" if normalise else "four_stored")
            stats[f"quaternion_{kind}"] += 1
            stats[f"quaternion_{kind}_worst_1e6"] = max(stats[f"quaternion_{kind}_worst_1e6"], round(worst * 1e6))
        out[track] = frames
    return out


def decode_animation(s, anim, stats=None):
    """Concatenate the sequences. Consecutive sequences share one frame, so sum(frames) == total + n - 1 and the
    first frame of every sequence after the first is dropped (and compared with the frame it repeats)."""
    tracks, total, frames_sum = {}, 0, 0
    for si, at in enumerate(anim["sequences"]):
        nf, channels = decode_sequence(s, at, stats)
        frames_sum += nf
        if stats is not None:
            for t, *_ in channels:
                stats[f"channel:{t:02d}_{CHANNEL_NAMES[t]}"] += 1
        part = assemble_tracks(nf, channels, len(anim["tracks"]), stats)
        for k, frames in part.items():
            if si and stats is not None and k in tracks and frames:
                a, b = tracks[k][-1], frames[0]
                if anim["tracks"][k][2] == 0 and len(a) == 4 == len(b):
                    stats["boundary_quaternions"] += 1
                    stats["boundary_quaternions_same_rotation"] += abs(sum(x * y for x, y in zip(a, b))) > 0.9999
                else:
                    stats["boundary_values"] += 1
                    stats["boundary_values_within_1e-3"] += max(abs(x - y) for x, y in zip(a, b)) < 1e-3
            have = tracks.setdefault(k, [frames[0]] * total if frames else [])
            have.extend(frames[1:] if si else frames)
        total += nf - (1 if si else 0)
        for k, have in tracks.items():
            if len(have) < total:
                have.extend([have[-1]] * (total - len(have)))
    if stats is not None:
        stats["frames_sum_matches"] += frames_sum == anim["frames"] + len(anim["sequences"]) - 1
    return tracks, total


# ---------------------------------------------------------------- resources


def is_root(head):
    """A clip-dictionary root page: 8 zero bytes, a 0x50xxxxxx pointer at +8, zeros from +0x10 to +0x38."""
    return (len(head) >= 0x40 and head[:8] == bytes(8) and head[16:0x38] == bytes(0x28)
            and struct.unpack_from("<Q", head, 8)[0] >> 28 == 5)


def _named_root_hash(page):
    """Return the verified page-local name hash shared by dictionary and pack headers."""
    if len(page) < 0x6C or not is_root(page):
        return None
    s = System(page)
    try:
        name = s.string(0x38)
    except DecodeError:
        return None
    stored = s.u32(0x68)
    return stored if name and anim_index.joaat32(name) == stored else None


def _local_packs(data):
    """Referenced (system pointer, name hash) pairs, or None until the table is available."""
    if len(data) < 0x7a:
        return None
    refs, count = struct.unpack_from("<QH", data, 0x70)
    if not count or refs < SYSTEM:
        return set()
    start = refs - SYSTEM
    end = start + count * 16
    if end > len(data):
        return None
    return {(ptr, name_hash) for ptr, name_hash in struct.iter_unpack("<QI4x", memoryview(data)[start:end])
            if ptr}


def _inflate(m, row):
    return zlib.decompressobj().decompress(m[row["offset"]:row["offset"] + row["stored"]])


def _next_to(prev, row):
    return row["offset"] == (prev["offset"] + prev["stored"] + 15) & ~15


def _gap_page(m, row, first):
    """The page right after `row` when it is not a listed stream: a zlib stream that inflates to fewer than the
    64 bytes anim_index lists, or a 16-byte raw tail (as rpf9-explorer reads). It is used only when the joined
    pages parse as a clip dictionary. -> segment dict or None."""
    d = zlib.decompressobj()
    d.decompress(m[row["offset"]:row["offset"] + row["stored"]])
    end = row["offset"] + row["stored"] - len(d.unused_data)
    at = (end + 15) & ~15
    if at + 16 > len(m) or m[end:at] != bytes(at - end):
        return None
    if anim_index.ZHDR.match(m, at) and m[at:at + 3] != b"KB2":
        z, chunk = zlib.decompressobj(), m[at:at + 4096]
        try:
            page = z.decompress(chunk)
        except zlib.error:
            return None
        if not z.eof:
            return None
        seg = {"offset": at, "stored": len(chunk) - len(z.unused_data), "raw": False, "kind": "small zlib page"}
    else:
        page = bytes(m[at:at + 16])
        seg = {"offset": at, "stored": 16, "raw": True, "kind": "raw 16-byte page"}
    try:
        parse_dictionary(System(first + page))
    except (DecodeError, struct.error, IndexError):
        return None
    return seg


def segment(rows, k):
    """(offset, stored, raw) for a stream index or a gap page."""
    if isinstance(k, dict):
        return k["offset"], k["stored"], k["raw"]
    return rows[k]["offset"], rows[k]["stored"], False


# A continuation can start with pack:/ paths or bone names inside the dictionary.
_MAP_BOUNDARIES = anim_index.STOP_SIGNATURES - {"name-table", "skeleton-names"}


def group(m, rows, *, first_row_only: bool = False, continuation_rows=None):
    """-> ([(root index, [stream indices or gap pages])], unresolved count by reason, gap pages by kind). A
    dictionary takes the page map's system page count of back-to-back streams. When the last page is not the next
    listed stream, the bytes right after the stream are tried (`_gap_page`); otherwise the root is reported.
    With first_row_only, later rows supply continuation pages but cannot start other dictionaries.
    A selected-root caller can supply continuation_rows to discover rows on demand."""
    def has_next(k):
        if k + 1 < len(rows):
            return True
        row = next(continuation_rows, None) if continuation_rows is not None else None
        if row is None:
            return False
        rows.append(row)
        return True

    out, unresolved, taken, gaps = [], collections.Counter(), set(), collections.Counter()
    for i, row in enumerate(rows[:1] if first_row_only else rows):
        if i in taken or row["signature"] != "zero-prefix":
            continue
        data = _inflate(m, row)
        if not is_root(data):
            continue
        pm = struct.unpack_from("<Q", data, 8)[0] - SYSTEM
        parts, k = [i], i
        def separate_root(page, offset):
            name_hash = _named_root_hash(page)
            if name_hash is None:
                return None  # Unnamed: use the existing page-map discovery rules.
            # Only pages already accepted for this owner may establish membership.
            # The candidate and its successors could belong to a different dictionary.
            packs = _local_packs(data)
            if packs is None and len(parts) > 1:
                prefix = data + b"".join(_inflate(m, rows[j]) for j in parts[1:])
                packs = _local_packs(prefix)
            return packs is None or (SYSTEM + offset, name_hash) not in packs

        # Only the two count bytes are needed. Locate them by inflated size,
        # including when they straddle a listed stream boundary.
        count_at = pm + 8
        counts = data[count_at:count_at + 2]
        end = len(data)
        while len(counts) < 2 and len(parts) < 255 and has_next(k):
            following = rows[k + 1]
            if not _next_to(rows[k], following) or following["signature"] in _MAP_BOUNDARIES:
                break
            start, end = end, end + following["size"]
            page = None
            if following["signature"] == "zero-prefix":
                page = _inflate(m, following)
                # Short continuation pages can resemble an unnamed root. Within
                # the original four-page window, check the root name or local map.
                if is_root(page) and (len(parts) >= 4 or any(page[0x38:0x40])
                                      or struct.unpack_from("<Q", page, 8)[0] - SYSTEM + 10 <= len(page)):
                    if separate_root(page, start) is not False:
                        break
            k += 1
            parts.append(k)
            if end > count_at:
                if page is None:
                    page = _inflate(m, following)
                counts += page[max(0, count_at - start):count_at + 2 - start]
        if len(counts) != 2:
            unresolved["page map not in the next stream"] += 1
            continue
        pages, graphics = counts
        if graphics:
            unresolved["graphics pages"] += 1
            continue
        while len(parts) < pages and has_next(k) and _next_to(rows[k], rows[k + 1]):
            following = rows[k + 1]
            if following["signature"] == "zero-prefix" and separate_root(_inflate(m, following), end):
                break
            end += following["size"]
            k += 1
            parts.append(k)
        # A listed adjacent root cannot also be an unlisted gap page.
        if len(parts) == pages - 1 and (k + 1 == len(rows) or not _next_to(rows[k], rows[k + 1])):
            joined = b"".join(_inflate(m, rows[j]) for j in parts)
            gap = _gap_page(m, rows[k], joined)
            if gap is not None:
                parts.append(gap)
        if len(parts) != pages:
            unresolved["system page not the next stream"] += 1
            continue
        if isinstance(parts[-1], dict):
            gaps[parts[-1]["kind"]] += 1
        taken.update(p for p in parts if not isinstance(p, dict))
        out.append((i, parts))
    return out, unresolved, gaps


def child_times(c):
    """A `.motion`'s +0x70 entries lined up with its children (None where they don't line up)."""
    times = c.get("times")
    return times if times is not None and len(times) == len(c["children"]) else [None] * len(c["children"])


def count_motion_blend(c, stats):
    """Counts for a `.motion`'s +0x70 child times and its parameter node (no values)."""
    times = c.get("times")
    if times is None:
        stats["motion_times_unreadable"] += 1
    else:
        stats["motion_times_one_per_child"] += len(times) == len(c["children"])
        for duration, blend, points in times:
            stats["motion_child_times"] += 1
            if len(points) == 2 and points == [(0.0, 0.0), (duration, blend)] and duration == blend:
                stats["motion_child_times_identity"] += 1
            elif (len(points) > 2 and points[0] == (0.0, 0.0) and points[-1] == (duration, blend) and
                  all(a[0] <= b[0] and a[1] <= b[1] for a, b in zip(points, points[1:]))):
                stats["motion_child_times_warped"] += 1
            else:
                stats["motion_child_times_other"] += 1
    node = c.get("node")
    if node is None:
        stats["motion_node_unreadable"] += 1
        return
    kind, axis = node
    stats[f"motion_node_type:{kind}"] += 1
    if axis is None:
        return
    count_motion_samples(c, kind, axis, stats)
    if kind != 3:
        stats[f"motion_node_axes:{kind}:{len(axis['names'])}"] += 1
        if kind != 1:
            pairs = axis["pairs"]
            stats[f"motion_node_pairs:{kind}:" + ("null" if pairs is None else str(len(pairs)))] += 1
        for h in axis["names"]:
            stats[f"motion_node_axis_name:{kind}:" + AXIS_NAMES.get(h, "unresolved")] += 1
        return
    values = [v for _i, v in axis["samples"]]
    stats["motion_axes"] += 1
    stats["motion_axis_samples"] += len(values)
    stats["motion_axis_one_name"] += len(axis["names"]) == 1
    stats["motion_axis_sorted"] += all(a <= b for a, b in zip(values, values[1:]))
    stats["motion_axis_range_is_first_last"] += bool(values) and (axis["min"], axis["max"]) == (values[0], values[-1])
    stats["motion_axis_indices_are_0_to_n"] += sorted(i for i, _v in axis["samples"]) == list(range(len(values)))
    stats["motion_axis_samples_equal_children"] += len(values) == len(c["children"] or [])
    stats[f"motion_axis_sample_count:{len(values)}"] += 1
    for h in axis["names"]:
        stats["motion_axis_name:" + AXIS_NAMES.get(h, f"0x{h:08x}")] += 1


def count_motion_samples(c, kind, axis, stats):
    """Counts for the +0x90 sample lists of a one-node `.motion` (+0x58 == 1) against its node (no values)."""
    lists = c.get("samples")
    if c["parameters"] != 1 or lists is None:
        return
    children = len(c["children"])
    mapped = [items[0][0] for items in lists if items]
    pre = f"motion_samples:{kind}:"
    stats[pre + "motions"] += 1
    stats[pre + "lists_equal_node_samples"] += len(lists) == axis["sample_count"]
    stats[pre + "lists_more_than_children"] += len(lists) > children
    stats[pre + "every_list_one_item"] += all(len(items) == 1 for items in lists)
    stats[pre + "children_in_range"] += all(ch < children for items in lists for ch, _a, _b in items)
    stats[pre + "every_child_sampled"] += {ch for items in lists for ch, _a, _b in items} == set(range(children))
    stats[pre + "sample_k_is_child_k"] += (len(lists) == children and all(len(items) == 1 for items in lists) and
                                           mapped == list(range(children)))
    for items in lists:
        for _ch, a, b in items:
            stats[pre + "items"] += 1
            stats[pre + "items_first_float_1"] += a == 1.0
            stats[pre + "items_second_float_1"] += b == 1.0


def new_stats():
    return collections.Counter()


def merge(total, part):
    """Add counters; `*_worst_1e6` entries keep the maximum."""
    for k, v in part.items():
        total[k] = max(total[k], v) if k.endswith("_worst_1e6") else total[k] + v


def decode_resource(path, segments, budget=None):
    """Worker: inflate and decode one dictionary. -> (row, stats). The row carries no values.
    With `budget` (seconds), animations left when it runs out are counted as skipped and the row says so."""
    started = time.monotonic()
    stats = new_stats()
    with open(path, "rb") as fh:
        data = b""
        for off, stored, raw in segments:
            fh.seek(off)
            data += fh.read(stored) if raw else zlib.decompressobj().decompress(fh.read(stored))
    s = System(data)
    row = {"offset": segments[0][0], "streams": len(segments), "system_bytes": len(data)}
    try:
        d = parse_dictionary(s)
    except NotClipDictionary as e:
        stats["roots_not_clip_dictionary:" + str(e)] += 1
        row["kind"] = str(e)
        return row, stats
    except (DecodeError, struct.error) as e:
        stats["dictionaries_unparsed"] += 1
        stats["dictionary_error:" + str(e).split(" at 0x")[0].split("pointer 0x")[0][:60]] += 1
        row["error"] = str(e)[:120]
        return row, stats
    stats["dictionaries"] += 1
    stats["dictionary_hash_matches_name"] += d["name"] is not None and anim_index.joaat32(d["name"]) == d["hash"]
    stats["clips"] += len(d["clips"])
    stats["clips_motion"] += sum(c["kind"] == "motion" for c in d["clips"])
    stats["clips_motionmatcher"] += sum(c["kind"] == "motionmatcher" for c in d["clips"])
    external = set(d["external_hashes"])
    for c in d["clips"]:
        if c["kind"] != "motion":
            continue
        if c["children"] is None:
            stats["motion_children_unreadable"] += 1
            continue
        stats[f"motion_parameters:{c['parameters']}"] += 1
        stats["motion_children"] += len(c["children"])
        stats["motion_children_in_external_refs"] += sum(dh in external for _ch, dh in c["children"])
        count_motion_blend(c, stats)
    stats["clip_animation_refs"] += sum(len(c["animations"]) for c in d["clips"])
    stats["packs_local"] += len(d["packs"])
    stats["packs_external"] += d["external_packs"]
    stats["dictionaries_without_local_pack"] += not d["packs"]
    ok = failed = skipped = 0
    for pack in d["packs"]:
        for anim in pack["animations"]:
            stats["animations"] += 1
            if budget is not None and time.monotonic() - started >= budget:  # >=: a coarse clock (Windows) reads 0.0 for short spans
                skipped += 1
                continue
            stats["sequences"] += len(anim["sequences"])
            for bone, ttype, fmt in anim["tracks"]:
                stats["track_type:" + TRACK_TYPES.get(ttype, f"0x{ttype:08x}")] += 1
                stats["track_format:" + TRACK_FORMATS.get(fmt, str(fmt))] += 1
            try:
                local = new_stats()
                decode_animation(s, anim, local)
                merge(stats, local)
                ok += 1
            except (DecodeError, struct.error, IndexError) as e:
                failed += 1
                stats["animation_error:" + str(e).split(" at 0x")[0][:60]] += 1
    stats["animations_decoded"] += ok
    stats["animations_failed"] += failed
    if skipped:
        stats["animations_skipped_over_budget"] += skipped
        stats["dictionaries_over_budget"] += 1
        row["over_budget"] = True
    # for the archive-wide join in scan(); scan() removes these lists from the rows it returns
    row["_clip_hashes"] = [c["hash"] for c in d["clips"] if c["kind"] == "clip"]
    row["_clip_durations"] = [c["duration"] for c in d["clips"] if c["kind"] == "clip"]
    row["_children"] = [ch for c in d["clips"] if c["kind"] == "motion" and c["children"] for ch in c["children"]]
    row["_child_durations"] = [t[0] if t else None for c in d["clips"] if c["kind"] == "motion" and c["children"]
                               for t in child_times(c)]
    row["_external"] = d["external_hashes"]
    row.update(hash=f"0x{d['hash']:08x}", name=d["name"], clips=len(d["clips"]), packs=len(d["packs"]),
               external_packs=d["external_packs"], animations=ok + failed + skipped, decoded=ok,
               seconds=round(time.monotonic() - started, 3))
    return row, stats


def join_references(rows, stats):
    """Resolve hash-only references across the archive: a root `+0x70` item with a null pointer names another
    dictionary (its joaat32 name hash), and a `.motion` child is {clip hash, dictionary hash}."""
    clips_by_dict = collections.defaultdict(set)
    duration = {}
    for r in rows:
        if "_clip_hashes" in r:
            clips_by_dict[int(r["hash"], 16)].update(r["_clip_hashes"])
            for h, t in zip(r["_clip_hashes"], r.get("_clip_durations", [])):
                duration[(int(r["hash"], 16), h)] = t
    ext_seen = set()
    for r in rows:
        for h in r.pop("_external", []):
            stats["external_refs_to_a_dictionary_here"] += h in clips_by_dict
            if h not in ext_seen:
                ext_seen.add(h)
                stats["external_hashes_distinct"] += 1
                stats["external_hashes_distinct_found_here"] += h in clips_by_dict
        kids = r.pop("_children", [])
        for (ch, dh), t in zip(kids, r.pop("_child_durations", [None] * len(kids))):
            if dh in clips_by_dict:
                stats["motion_children_dictionary_here"] += 1
                stats["motion_children_resolved_to_clip"] += ch in clips_by_dict[dh]
                if ch in clips_by_dict[dh] and t is not None:
                    stats["motion_child_duration_equals_clip"] += duration.get((dh, ch)) == t
        r.pop("_clip_hashes", None)
        r.pop("_clip_durations", None)


def _decode_star(args):
    return decode_resource(*args)


def scan(path, start=None, limit=None, jobs=1, progress=None, budget=None):
    rows, walk = anim_index.scan_rpf(path, start, limit)
    with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as m:
        groups, unresolved, gaps = group(m, rows)
    work = [(path, [segment(rows, k) for k in parts], budget) for _i, parts in groups]
    used = [k for _i, parts in groups for k in parts if not isinstance(k, dict)]
    stats, out = new_stats(), []
    if jobs > 1 and len(work) > 1:
        import multiprocessing
        with multiprocessing.Pool(process_workers(jobs)) as pool:
            for n, (row, st) in enumerate(pool.imap_unordered(_decode_star, work, chunksize=4), 1):
                out.append(row)
                merge(stats, st)
                if progress and n % 5000 == 0:
                    progress(n, len(work))
    else:
        for n, w in enumerate(work, 1):
            row, st = decode_resource(*w)
            out.append(row)
            merge(stats, st)
            if progress and n % 5000 == 0:
                progress(n, len(work))
    out.sort(key=lambda r: r["offset"])
    join_references(out, stats)
    summary = {
        "streams": walk["files"],
        "dictionary_roots": len(groups) + sum(unresolved.values()),
        "dictionaries_grouped": len(groups),
        "unresolved": dict(unresolved),
        "gap_pages": dict(gaps),
        "pages": {str(k): v for k, v in sorted(collections.Counter(len(p) for _i, p in groups).items())},
        "dictionary_streams": len(used),
        "dictionary_stored_bytes": sum(rows[k]["stored"] for k in used),
        "dictionary_inflated_bytes": sum(rows[k]["size"] for k in used),
        "dictionary_stream_signatures": dict(collections.Counter(
            rows[k]["signature"].split(":")[0] for k in used).most_common()),
        "slowest_dictionary_seconds": max([r.get("seconds", 0) for r in out] or [0]),
        "counts": dict(sorted(stats.items())),
    }
    return out, summary


def decode_one(path, offset, frames):
    rows, _walk = anim_index.scan_rpf(path, offset, 64 << 20)
    with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as m:
        groups, _unresolved, _gaps = group(m, rows, first_row_only=True)
    if not groups or rows[groups[0][0]]["offset"] != offset:
        raise ValueError(f"no clip dictionary starts at 0x{offset:x}")
    with open(path, "rb") as fh:
        data = b""
        for k in groups[0][1]:
            off, stored, raw = segment(rows, k)
            fh.seek(off)
            data += fh.read(stored) if raw else zlib.decompressobj().decompress(fh.read(stored))
    s = System(data)
    d = parse_dictionary(s)
    for pack in d["packs"]:
        for anim in pack["animations"]:
            try:
                tracks, total = decode_animation(s, anim)
                anim["decoded"] = {str(k): [list(v) for v in vals[:frames]] for k, vals in tracks.items()}
                anim["decoded_frames"] = total
            except (DecodeError, struct.error, IndexError) as e:
                anim["error"] = str(e)
            anim["tracks"] = [{"bone": f"0x{b:08x}", "type": TRACK_TYPES.get(t, f"0x{t:08x}"),
                               "format": TRACK_FORMATS.get(f, f)} for b, t, f in anim["tracks"]]
            anim.pop("sequences")
    for clip in d["clips"]:
        for a in clip["animations"]:
            a.pop("animation")
    return d


def same_output_path(first, second):
    """True when writing ``second`` would truncate ``first`` (same file, path or hard link)."""
    if os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second)):
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("archive", help="anim.rpf")
    ap.add_argument("out", nargs="?", help="output .json (counts and one row per dictionary)")
    ap.add_argument("--start", type=anim_index.offset, help="first byte to scan (default: the RPF data start)")
    ap.add_argument("--limit", type=anim_index.byte_count, help="number of bytes to scan")
    ap.add_argument("--jobs", type=int, default=1, help="worker processes for decoding (default 1)")
    ap.add_argument("--budget", type=float, default=600.0,
                    help="seconds per dictionary; animations left after it are counted as skipped (default 600)")
    ap.add_argument("--decode", type=anim_index.offset, metavar="OFFSET",
                    help="print one dictionary (root stream offset) with decoded values to stdout")
    ap.add_argument("--frames", type=int, default=4, help="frames per track printed by --decode (default 4)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if a.jobs < 1:
        ap.error("--jobs must be at least 1")
    if a.frames < 0:
        ap.error("--frames must be 0 or more")
    if math.isnan(a.budget) or a.budget < 0:
        ap.error("--budget must be 0 or a finite positive value (or infinite)")
    if a.decode is None and not a.out:
        ap.error("an output .json is required unless --decode is given")
    if a.out:
        if os.path.isdir(a.out):
            ap.error(f"{a.out} is a directory, not a .json file")
        if same_output_path(a.archive, a.out):
            ap.error("output must not overwrite the input archive")
    try:
        if a.decode is not None:
            json.dump(decode_one(a.archive, a.decode, a.frames), sys.stdout, indent=1)
            print()
            return 0

        def progress(n, total):
            if not a.quiet:
                print(f"[{n / max(1, total):6.1%}] {n} dictionaries", flush=True)
        rows, summary = scan(a.archive, a.start, a.limit, a.jobs, progress, a.budget)
    except ValueError as e:
        sys.exit(f"{a.archive}: {e}")
    except OSError as e:
        sys.exit(f"{e.filename or a.archive}: {e.strerror or e}")
    try:
        parent = os.path.dirname(os.path.abspath(a.out))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"summary": summary, "dictionaries": rows}, f, indent=1)
    except OSError as e:
        sys.exit(f"{a.out}: cannot write: {e.strerror or e}")
    c = summary["counts"]
    print(json.dumps({k: summary[k] for k in ("streams", "dictionary_roots", "dictionaries_grouped", "unresolved")}))
    print(json.dumps({k: c.get(k, 0) for k in ("dictionaries", "clips", "animations", "animations_decoded",
                                                "animations_failed", "sequences")}))
    return 0


if __name__ == "__main__":
    try:  # a redirected stdout is strict ANSI code page under -I; write '?' for what it cannot encode
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    sys.exit(main())
