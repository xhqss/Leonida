"""Synthetic tests for tools/anim_clips.py.

    python -I tests/test_anim_clips.py

Every byte here is built by the test: a fake RPF9 data area holding clip-dictionary resources made by the
encoder below (dictionary root, clip, animation pack, animation, sequences and every decoded channel type,
including a type-10 channel). No game file or game asset name is used.
"""
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "anim_clips.py")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import anim_clips  # noqa: E402
from anim_index import joaat32  # noqa: E402

SYSTEM = 0x50000000
BONEROTATION = joaat32("bonerotation")
BONETRANSLATION = joaat32("bonetranslation")
GENERICCONTROL = joaat32("genericcontrol")


# ---------------------------------------------------------------- encoders


class Bits:
    """LSB-first bit writer."""

    def __init__(self):
        self.value, self.n = 0, 0

    def put(self, x, k):
        assert 0 <= x < (1 << k) or k == 0 and x == 0
        self.value |= x << self.n
        self.n += k

    def bytes(self, length=None):
        length = (self.n + 7) // 8 if length is None else length
        return self.value.to_bytes(length, "little")


def zz(v):
    return 2 * v if v >= 0 else -2 * v - 1


def encode_delta2(ints, quantum, offset):
    """Type-10 channel for non-negative integers `ints` (value = int * quantum + offset)."""
    nf = len(ints)
    nc = (nf + 63) // 64
    starts = [ints[64 * c] for c in range(nc)]
    raw = []
    for c in range(nc):
        v = 0
        for j in range(64):
            f = 64 * c + j + 1
            if f >= nf:
                break
            step = ints[f] - ints[f - 1]
            raw.append(zz(step - v))
            v = step
    assert len(raw) == nf - 1
    raw += [0] * ((-len(raw)) % 8)
    widths = [max(x.bit_length() for x in raw[g:g + 8]) for g in range(0, len(raw), 8)]
    total = sum(widths)
    c3 = (total + 31) // 32
    positions, acc = [], 0
    for c in range(nc):
        positions.append(acc)
        acc += sum(widths[8 * c:8 * c + 8])
    c1 = max(1, max(positions).bit_length())
    c2 = max(1, max(starts).bit_length())
    front = Bits()
    for p in positions:
        front.put(p, c1)
    for s in starts:
        front.put(s, c2)
    for k in widths:
        front.put(0, k)
        front.put(1, 1)
    lanes = [Bits() for _ in range(8)]
    for g, k in enumerate(widths):
        for j in range(8):
            lanes[j].put(raw[8 * g + j], k)
    front_bytes = front.bytes(((front.n + 31) // 32) * 4)
    words = []
    lane_words = [lanes[j].value.to_bytes(4 * c3, "little") for j in range(8)]
    for w in range(c3):
        for j in range(8):
            words.append(lane_words[j][4 * w:4 * w + 4])
    blob = front_bytes + b"".join(words)
    head = struct.pack("<IIff", (16 + len(blob)) // 4, c1 | c2 << 8 | c3 << 16, quantum, offset)
    return head + blob


def encode_sequence(nf, channels):
    """channels: list of (type, track, comp, spec). spec per type:
    0/1: (x, y, z); 2: f; 3: [f32 per frame]; 4: (bits, quantum, offset, [ints]);
    5: (frame_bits, value_bits, [table ints], quantum, offset, [indices]); 7/8: None; 10: (quantum, offset, [ints]).
    """
    by_type = {t: [] for t in range(11)}
    for ch in channels:
        by_type[ch[0]].append(ch)
    defs = bytearray()
    frame_fields = []
    for t in range(11):
        for _t, _track, _comp, spec in by_type[t]:
            if t in (0, 1):
                defs += struct.pack("<3f", *spec)
            elif t == 2:
                defs += struct.pack("<f", spec)
            elif t == 3:
                frame_fields.append((32, [struct.unpack("<I", struct.pack("<f", v))[0] for v in spec]))
            elif t == 4:
                bits, quantum, offset, ints = spec
                defs += struct.pack("<Iff", bits, quantum, offset)
                frame_fields.append((bits, ints))
            elif t == 5:
                fbits, vbits, table, quantum, offset, idx = spec
                tb = Bits()
                for x in table:
                    tb.put(x, vbits)
                words = (tb.n + 31) // 32
                defs += struct.pack("<IIIff", fbits, vbits, words, quantum, offset) + tb.bytes(4 * words)
                frame_fields.append((fbits, idx))
            elif t == 10:
                defs += encode_delta2(spec[2], spec[0], spec[1])
    # type-4 bits are summed in the header; frame fields go type 3, then 4, then 5 (the order above)
    qbits = sum(spec[0] for t, _tr, _c, spec in channels if t == 4)
    need = sum(b for b, _ in frame_fields)
    fl = ((need + 31) // 32) * 4
    frames = bytearray()
    for f in range(nf):
        row = Bits()
        for bits, vals in frame_fields:
            row.put(vals[f], bits)
        frames += row.bytes(fl)
    counts = [len(by_type[t]) for t in range(11)]
    lists = bytearray()
    for t in range(11):
        entries = [track << 2 | comp for _t, track, comp, _s in by_type[t]]
        entries += [0] * ((-len(entries)) % 4)
        lists += struct.pack(f"<{len(entries)}H", *entries)
    data = bytes(defs) + bytes(frames) + struct.pack("<11H", *counts) + bytes(lists)
    header = struct.pack("<5I5H2B", 0x1234, len(data), 0, len(defs), 0x20 + len(data), 2, nf, fl, 0, qbits, 11, 0)
    return header + data


class Page:
    """System-page builder: offsets are page offsets, pointers are 0x50000000 + offset."""

    def __init__(self):
        self.b = bytearray(0x80)

    def alloc(self, data, align=16):
        self.b += bytes((-len(self.b)) % align)
        at = len(self.b)
        self.b += data
        return at

    def ptr(self, at, target):
        struct.pack_into("<Q", self.b, at, 0 if target is None else SYSTEM + target)

    def string(self, at, text):
        p = self.alloc(text.encode() + b"\x00")
        self.ptr(at, p)
        struct.pack_into("<HH", self.b, at + 8, len(text), len(text) + 1)

    def atmap(self, at, items):
        """items: [(key, value offset)] spread over 11 buckets."""
        buckets = self.alloc(bytes(8 * 11))
        heads = [None] * 11
        for key, value in items:
            e = self.alloc(bytes(0x18))
            struct.pack_into("<Q", self.b, e, key)
            self.ptr(e + 8, value)
            self.ptr(e + 16, heads[key % 11])
            heads[key % 11] = e
        for i, h in enumerate(heads):
            self.ptr(buckets + 8 * i, h)
        self.ptr(at, buckets)
        struct.pack_into("<I", self.b, at + 8, 11)
        struct.pack_into("<I", self.b, at + 0x10, len(items))


def animation(page, frames, tracks, sequences):
    a = page.alloc(bytes(0x70))
    struct.pack_into("<H", page.b, a + 0x24, frames)
    struct.pack_into("<f", page.b, a + 0x28, (frames - 1) / 30.0)
    seq_ptrs = page.alloc(bytes(8 * len(sequences)))
    for i, s in enumerate(sequences):
        page.ptr(seq_ptrs + 8 * i, page.alloc(s))
    page.ptr(a + 0x50, seq_ptrs)
    struct.pack_into("<HH", page.b, a + 0x58, len(sequences), len(sequences))
    tr = page.alloc(b"".join(struct.pack("<3I", *t) for t in tracks))
    page.ptr(a + 0x60, tr)
    struct.pack_into("<HH", page.b, a + 0x68, len(tracks), len(tracks))
    return a


def dictionary(name="synthetic@dict", sequences=None, frames=None, tracks=None, external=0, children=(),
               external_hashes=(), parameters=1, times=None, axis=None, node=None, sample_lists=None,
               node_axis_count=None):
    """One clip dictionary page: one `.clip`, one `.motion`, one `.motionmatcher`, one local pack with one animation."""
    p = Page()
    pm = p.alloc(bytes(16))
    p.b[pm + 8], p.b[pm + 9] = 1, 0
    p.ptr(8, pm)
    p.string(0x38, name)
    struct.pack_into("<I", p.b, 0x68, joaat32(name))
    anim = animation(p, frames, tracks, sequences)
    pack = p.alloc(bytes(0x70))
    p.string(pack + 0x38, name + "-0")
    struct.pack_into("<I", p.b, pack + 0x68, joaat32(name + "-0"))
    p.atmap(pack + 0x48, [(joaat32("one"), anim)])
    clip = p.alloc(bytes(0x80))
    struct.pack_into("<I", p.b, clip + 0x20, 2)
    p.string(clip + 0x28, "pack:/one.clip")
    struct.pack_into("<f", p.b, clip + 0x78, (frames - 1) / 30.0)
    entries = p.alloc(bytes(0x38))
    p.ptr(entries, anim)
    struct.pack_into("<I", p.b, entries + 8, joaat32("one"))
    struct.pack_into("<3f", p.b, entries + 0x28, 0.0, (frames - 1) / 30.0, 1.0)
    lst = p.alloc(bytes(0x18))
    p.ptr(lst, entries)
    struct.pack_into("<HH", p.b, lst + 8, 1, 1)
    p.ptr(clip + 0x68, lst)
    motion = p.alloc(bytes(0x150))
    struct.pack_into("<I", p.b, motion + 0x20, 0x5000)
    p.string(motion + 0x30, "pack:/one.motion")
    struct.pack_into("<I", p.b, motion + 0x58, parameters)          # 1 or 2
    if children:                                                     # {u64 0; u32 clip hash; u32 dictionary hash}
        kids = p.alloc(b"".join(struct.pack("<QII", 0, ch, dh) for ch, dh in children))
        p.ptr(motion + 0x60, kids)
        struct.pack_into("<HH", p.b, motion + 0x68, len(children), len(children))
    if times is not None:      # +0x70: per child {f32 duration, f32 blend duration, (child time, blend time) curve}
        table = p.alloc(bytes(0x28 * len(times)))
        for i, (duration, blend, points) in enumerate(times):
            e = table + 0x28 * i
            struct.pack_into("<ff", p.b, e, duration, blend)
            p.ptr(e + 8, p.alloc(b"".join(struct.pack("<ff", x, y) for x, y in points)))
            struct.pack_into("<HH", p.b, e + 16, len(points), len(points))
        p.ptr(motion + 0x70, table)
        struct.pack_into("<HH", p.b, motion + 0x78, len(times), len(times))
    if axis is not None:       # a type-3 node: {u64 0, u32 3, u32 0, axes, (index, value) samples, f32 min, f32 max}
        names, samples, lo, hi = axis
        node_at = p.alloc(bytes(0x40))
        struct.pack_into("<I", p.b, node_at + 8, 3)
        p.ptr(node_at + 0x10, p.alloc(b"".join(struct.pack("<IHH", h, 0, 0) for h in names)))
        struct.pack_into("<HH", p.b, node_at + 0x18, len(names), len(names))
        p.ptr(node_at + 0x20, p.alloc(b"".join(struct.pack("<If", i, v) for i, v in samples)))
        struct.pack_into("<HH", p.b, node_at + 0x28, len(samples), len(samples))
        struct.pack_into("<ff", p.b, node_at + 0x30, lo, hi)
    elif node is not None:     # (type, [(name hash, u16, u16)], sample count): type 1 or a multi-axis node
        kind, axes, count = node
        node_at = p.alloc(bytes(0xB0))
        struct.pack_into("<I", p.b, node_at + 8, kind)
        if kind == 1:
            struct.pack_into("<I", p.b, node_at + 0x20, count)
        else:
            p.ptr(node_at + 0x10, p.alloc(b"".join(struct.pack("<IHH", *a) for a in axes)))
            n_axes = len(axes) if node_axis_count is None else node_axis_count   # override: an axis array overrun
            struct.pack_into("<HH", p.b, node_at + 0x18, n_axes, n_axes)
            struct.pack_into("<I", p.b, node_at + anim_clips.NODE_SAMPLE_COUNT_AT[kind], count)
    if axis is not None or node is not None:
        if parameters == 1:
            p.ptr(motion + 0x138, node_at)
        else:                  # +0x120 points at a block whose +0x18 is the node
            block = p.alloc(bytes(0x20))
            p.ptr(block + 0x18, node_at)
            p.ptr(motion + 0x120, block)
            struct.pack_into("<HH", p.b, motion + 0x128, 1, 1)
    if sample_lists is not None:   # +0x90: per sample {ptr, u16 count} of {u32 child, f32, f32}
        table = p.alloc(bytes(16 * len(sample_lists)))
        for i, items in enumerate(sample_lists):
            p.ptr(table + 16 * i, p.alloc(b"".join(struct.pack("<Iff", *it) for it in items)))
            struct.pack_into("<HH", p.b, table + 16 * i + 8, len(items), len(items))
        p.ptr(motion + 0x90, table)
        struct.pack_into("<HH", p.b, motion + 0x98, len(sample_lists), len(sample_lists))
    matcher = p.alloc(bytes(0x50))
    p.ptr(matcher + 0x20, motion)                     # a pointer at +0x20, like `.motion`
    p.b[matcher + 0x28:matcher + 0x38] = bytes(range(0x80, 0x90))   # 16-byte id; +0x30 is not a pointer
    p.string(matcher + 0x38, "pack:/one.motionmatcher")
    p.atmap(0x48, [(joaat32(name) << 32 | 1, clip), (joaat32(name) << 32 | 2, motion),
                   (joaat32(name) << 32 | 3, matcher)])
    hashes = [0x1000 + i for i in range(external)] + list(external_hashes)
    refs = p.alloc(bytes(16 * (1 + len(hashes))))
    p.ptr(refs, pack)
    struct.pack_into("<I", p.b, refs + 8, joaat32(name + "-0"))
    for i, h in enumerate(hashes):
        struct.pack_into("<I", p.b, refs + 16 * (i + 1) + 8, h)
    p.ptr(0x70, refs)
    struct.pack_into("<HH", p.b, 0x78, 1 + len(hashes), 1 + len(hashes))
    p.b += bytes((-len(p.b)) % 16)
    return bytes(p.b)


# Track 0: rotation, rebuilt (type 7 at comp 3) from x/y/z: x quantized, y static float, z type 10.
# Track 1: rotation, type 8 with four stored components. Track 2: translation, static vec3.
# Track 3: control, indirect (type 5). Track 4: control, raw f32 (type 3).
NF = 70
Z_INTS = [(f * f) // 50 % 97 + 3 for f in range(NF)]
X_INTS = [(5 * f) % 64 for f in range(NF)]
TRACKS = [(joaat32("skel_a"), BONEROTATION, 0), (joaat32("skel_b"), BONEROTATION, 0),
          (joaat32("skel_c"), BONETRANSLATION, 2), (0x11, GENERICCONTROL, 4), (0x12, GENERICCONTROL, 4)]
TABLE = [0, 5, 9, 15]


def sequence(lo, hi):
    n = hi - lo
    q = 0.002
    return encode_sequence(n, [
        (4, 0, 0, (6, q, -0.05, X_INTS[lo:hi])),
        (2, 0, 1, 0.25),
        (10, 0, 2, (q, -0.1, Z_INTS[lo:hi])),
        (7, 0, 3, None),
        (2, 1, 0, 0.5), (2, 1, 1, 0.5), (2, 1, 2, 0.5), (2, 1, 3, 0.5), (8, 1, 0, None),
        (1, 2, 0, (1.0, 2.0, 3.0)),
        (5, 3, 0, (2, 4, TABLE, 0.1, 1.0, [f % 4 for f in range(lo, hi)])),
        (3, 4, 0, [float(f) * 1.5 for f in range(lo, hi)]),
    ])


def two_sequence_dictionary(name="synthetic@dict", **kw):
    # two sequences share frame 40
    return dictionary(name, [sequence(0, 41), sequence(40, NF)], NF, TRACKS, **kw)


def write_rpf(path, streams, gap_before=()):
    """streams: list of inflated payloads; each is zlib-compressed and 16-byte aligned, or preceded by a 4 KiB
    gap when its index is in `gap_before`."""
    header = bytearray(0x100)
    header[:4] = b"9FPR"
    struct.pack_into("<I", header, 4, len(header))
    body, offsets = bytearray(), []
    for i, payload in enumerate(streams):
        if i in gap_before:
            body += b"\x55" * 4096
        offsets.append(len(header) + len(body))
        body += zlib.compress(payload)
        body += bytes((-len(body)) % 16)
    body += bytes(64)
    with open(path, "wb") as f:
        f.write(header + body)
    return offsets


def split(page, at, move_map=True):
    """Two-page system: set the page count to 2 and, with `move_map`, put the page map on the second page."""
    b = bytearray(page)
    if move_map:
        pm = len(b)
        b += bytes(16)
        struct.pack_into("<Q", b, 8, SYSTEM + pm)
    else:
        pm = struct.unpack_from("<Q", b, 8)[0] - SYSTEM
    b[pm + 8], b[pm + 9] = 2, 0
    return bytes(b[:at]), bytes(b[at:])


# ---------------------------------------------------------------- tests


class DecodeTest(unittest.TestCase):
    def setUp(self):
        self.s = anim_clips.System(two_sequence_dictionary())
        self.d = anim_clips.parse_dictionary(self.s)

    def test_structure(self):
        d = self.d
        self.assertEqual(d["name"], "synthetic@dict")
        self.assertEqual(d["hash"], joaat32("synthetic@dict"))
        self.assertEqual(sorted((c["kind"], c["name"]) for c in d["clips"]),
                         [("clip", "pack:/one.clip"), ("motion", "pack:/one.motion"),
                          ("motionmatcher", "pack:/one.motionmatcher")])
        clip = [c for c in d["clips"] if c["kind"] == "clip"][0]
        self.assertEqual(clip["name"], "pack:/one.clip")
        self.assertEqual(len(clip["animations"]), 1)
        self.assertEqual(clip["animations"][0]["hash"], joaat32("one"))
        self.assertEqual(len(d["packs"]), 1)
        anim = d["packs"][0]["animations"][0]
        self.assertEqual(anim["frames"], NF)
        self.assertEqual(len(anim["sequences"]), 2)
        self.assertEqual(anim["tracks"], TRACKS)

    def test_motion_times_and_axis(self):
        f32 = lambda x: struct.unpack("<f", struct.pack("<f", x))[0]
        times = [(1.5, 1.5, [(0.0, 0.0), (1.5, 1.5)]), (2.0, 1.5, [(0.0, 0.0), (1.0, 0.5), (2.0, 1.5)])]
        samples = [(1, -1.5), (0, 1.5)]
        for parameters in (1, 2):
            d = anim_clips.parse_dictionary(anim_clips.System(two_sequence_dictionary(
                children=[(1, 2), (3, 4)], parameters=parameters, times=times,
                axis=([joaat32("speed")], samples, -1.5, 1.5))))
            motion = [c for c in d["clips"] if c["kind"] == "motion"][0]
            self.assertEqual(motion["parameters"], parameters)
            self.assertEqual(motion["times"], [(f32(a), f32(b), [(f32(x), f32(y)) for x, y in pts])
                                               for a, b, pts in times])
            kind, axis = motion["node"]
            self.assertEqual((kind, axis["names"], axis["samples"], axis["min"], axis["max"]),
                             (3, [joaat32("speed")], samples, -1.5, 1.5))
            stats = anim_clips.new_stats()
            anim_clips.count_motion_blend(motion, stats)
            self.assertEqual((stats["motion_child_times_identity"], stats["motion_child_times_warped"],
                              stats["motion_axes"], stats["motion_axis_sorted"],
                              stats["motion_axis_range_is_first_last"], stats["motion_axis_name:speed"]),
                             (1, 1, 1, 1, 1, 1))

    def test_motion_without_blend_data(self):
        motion = [c for c in self.d["clips"] if c["kind"] == "motion"][0]
        self.assertEqual((motion["times"], motion["node"]), ([], None))      # null +0x70, null +0x138
        self.assertEqual(motion["samples"], [])                              # null +0x90

    def motion(self, **kw):
        d = anim_clips.parse_dictionary(anim_clips.System(two_sequence_dictionary(**kw)))
        return [c for c in d["clips"] if c["kind"] == "motion"][0]

    def test_type3_samples_map_to_children(self):
        # three samples over two children: sample 0 and 2 play child 1, sample 1 plays child 0
        lists = [[(1, 1.0, 1.0)], [(0, 1.0, 0.5)], [(1, 1.0, 1.0)]]
        motion = self.motion(children=[(1, 2), (3, 4)], axis=([joaat32("speed")], [(0, 0.0), (2, 0.5), (1, 1.0)],
                                                              0.0, 1.0), sample_lists=lists)
        self.assertEqual(motion["samples"], lists)
        kind, axis = motion["node"]
        self.assertEqual((kind, axis["slots"], axis["sample_count"]), (3, [(0, 0)], 3))
        stats = anim_clips.new_stats()
        anim_clips.count_motion_blend(motion, stats)
        pre = "motion_samples:3:"
        self.assertEqual({k[len(pre):]: v for k, v in stats.items() if k.startswith(pre)},
                         {"motions": 1, "lists_equal_node_samples": 1, "lists_more_than_children": 1,
                          "every_list_one_item": 1, "children_in_range": 1, "every_child_sampled": 1,
                          "sample_k_is_child_k": 0, "items": 3, "items_first_float_1": 3,
                          "items_second_float_1": 2})

    def test_identity_samples(self):
        motion = self.motion(children=[(1, 2), (3, 4)], axis=([joaat32("speed")], [(0, 0.0), (1, 1.0)], 0.0, 1.0),
                             sample_lists=[[(0, 1.0, 1.0)], [(1, 1.0, 1.0)]])
        stats = anim_clips.new_stats()
        anim_clips.count_motion_blend(motion, stats)
        self.assertEqual(stats["motion_samples:3:sample_k_is_child_k"], 1)

    def test_several_items_in_a_list_are_not_identity(self):
        # list 0 holds children 0 and 1: its first child is 0, but that is not sample k -> child k
        motion = self.motion(children=[(1, 2), (3, 4)], axis=([joaat32("speed")], [(0, 0.0), (1, 1.0)], 0.0, 1.0),
                             sample_lists=[[(0, 1.0, 1.0), (1, 1.0, 1.0)], [(1, 1.0, 1.0)]])
        stats = anim_clips.new_stats()
        anim_clips.count_motion_blend(motion, stats)
        self.assertEqual((stats["motion_samples:3:sample_k_is_child_k"], stats["motion_samples:3:every_list_one_item"],
                          stats["motion_samples:3:every_child_sampled"]), (0, 0, 1))

    def test_malformed_node_keeps_the_time_table(self):
        times = [(1.5, 1.5, [(0.0, 0.0), (1.5, 1.5)])]
        motion = self.motion(children=[(1, 2)], times=times, node=(8, [(1, 0, 0)], 1), node_axis_count=0xFFFF)
        self.assertIsNone(motion["node"])                       # the axis array runs past the page
        self.assertEqual(len(motion["times"]), 1)

    def test_child_out_of_range_is_counted(self):
        motion = self.motion(children=[(1, 2)], axis=([joaat32("speed")], [(0, 0.0)], 0.0, 0.0),
                             sample_lists=[[(5, 1.0, 1.0)]])
        stats = anim_clips.new_stats()
        anim_clips.count_motion_blend(motion, stats)
        self.assertEqual((stats["motion_samples:3:children_in_range"], stats["motion_samples:3:every_child_sampled"]),
                         (0, 0))

    def test_multi_axis_nodes(self):
        for kind, n_axes in ((4, 2), (5, 3), (6, 4), (8, 5)):
            axes = [(joaat32("speed") if i == 0 else 0xABC0 + i, i, 1) for i in range(n_axes)]
            lists = [[(i % 2, 1.0, 1.0)] for i in range(4)]
            motion = self.motion(children=[(1, 2), (3, 4)], node=(kind, axes, 4), sample_lists=lists)
            k, axis = motion["node"]
            self.assertEqual((k, axis["names"], axis["slots"], axis["sample_count"]),
                             (kind, [a[0] for a in axes], [(i, 1) for i in range(n_axes)], 4))
            self.assertNotIn("samples", axis)
            stats = anim_clips.new_stats()
            anim_clips.count_motion_blend(motion, stats)
            self.assertEqual((stats[f"motion_node_axes:{kind}:{n_axes}"],
                              stats[f"motion_node_axis_name:{kind}:speed"],
                              stats[f"motion_node_axis_name:{kind}:unresolved"],
                              stats[f"motion_samples:{kind}:lists_equal_node_samples"],
                              stats[f"motion_samples:{kind}:every_child_sampled"], stats["motion_axes"]),
                             (1, 1, n_axes - 1, 1, 1, 0))

    def test_type1_node(self):
        motion = self.motion(children=[(1, 2)], node=(1, [], 1), sample_lists=[[(0, 1.0, 1.0)]])
        self.assertEqual(motion["node"], (1, {"names": [], "slots": [], "sample_count": 1}))
        stats = anim_clips.new_stats()
        anim_clips.count_motion_blend(motion, stats)
        self.assertEqual((stats["motion_node_type:1"], stats["motion_node_axes:1:0"],
                          stats["motion_samples:1:sample_k_is_child_k"]), (1, 1, 1))

    def test_sample_lists_of_a_two_node_motion_are_not_counted(self):
        motion = self.motion(children=[(1, 2)], parameters=2, node=(1, [], 1), sample_lists=[[(0, 1.0, 1.0)]] * 3)
        self.assertEqual(len(motion["samples"]), 3)
        stats = anim_clips.new_stats()
        anim_clips.count_motion_blend(motion, stats)
        self.assertFalse([k for k in stats if k.startswith("motion_samples:")])

    def test_delta2_round_trip(self):
        for n in (1, 2, 8, 9, 63, 64, 65, 129, 200):
            ints = [(7 * f * f) % 1000 + 20 * (f % 3) for f in range(n)]
            blob = encode_delta2(ints, 0.5, -3.0) + bytes(8)
            stats = anim_clips.new_stats()
            values, size = anim_clips.decode_delta2(blob, 0, n, stats)
            self.assertEqual(size, len(blob) - 8)
            self.assertEqual(values, [x * 0.5 - 3.0 for x in ints], n)
            self.assertEqual(stats["delta2_boundaries"], (n - 1) // 64)
            self.assertEqual(stats["delta2_boundaries_closed"], stats["delta2_boundaries"])

    def test_tracks(self):
        anim = self.d["packs"][0]["animations"][0]
        stats = anim_clips.new_stats()
        tracks, total = anim_clips.decode_animation(self.s, anim, stats)
        self.assertEqual(total, NF)
        self.assertEqual(stats["frames_sum_matches"], 1)
        for f in (0, 39, 40, 41, NF - 1):
            x, y, z = X_INTS[f] * 0.002 - 0.05, 0.25, Z_INTS[f] * 0.002 - 0.1
            w = math.sqrt(1 - x * x - y * y - z * z)
            for got, want in zip(tracks[0][f], (x, y, z, w)):
                self.assertAlmostEqual(got, want, places=5)
            self.assertEqual(len(tracks[1][f]), 4)
            for got in tracks[1][f]:
                self.assertAlmostEqual(got, 0.5, places=6)
            self.assertEqual(tracks[2][f], (1.0, 2.0, 3.0))
            self.assertAlmostEqual(tracks[3][f][0], TABLE[f % 4] * 0.1 + 1.0, places=5)
            self.assertEqual(tracks[4][f][0], f * 1.5)
        self.assertEqual(stats["boundary_quaternions"], stats["boundary_quaternions_same_rotation"])
        self.assertEqual(stats["boundary_values"], stats["boundary_values_within_1e-3"])
        self.assertEqual(stats["channel:10_delta2_float"], 2)
        self.assertEqual(stats["channel:03_raw_float"], 2)

    def test_rebuilt_component_goes_to_the_type7_position(self):
        seq = encode_sequence(1, [(2, 0, 0, 0.1), (2, 0, 1, 0.2), (2, 0, 2, 0.3), (7, 0, 1, None)])
        s = anim_clips.System(dictionary("p", [seq], 1, TRACKS[:1]))
        anim = anim_clips.parse_dictionary(s)["packs"][0]["animations"][0]
        tracks, _ = anim_clips.decode_animation(s, anim)
        w = math.sqrt(1 - 0.01 - 0.04 - 0.09)
        for got, want in zip(tracks[0][0], (0.1, w, 0.2, 0.3)):
            self.assertAlmostEqual(got, want, places=6)

    def test_definitions_must_end_at_the_frame_data(self):
        seq = bytearray(encode_sequence(3, [(2, 0, 0, 1.0), (2, 0, 1, 2.0)]))
        struct.pack_into("<H", seq, 0x20 + 8 + 2 * 2, 1)   # count says one static float, 8 definition bytes
        s = anim_clips.System(dictionary("p", [bytes(seq)], 3, TRACKS[3:4]))
        anim = anim_clips.parse_dictionary(s)["packs"][0]["animations"][0]
        with self.assertRaisesRegex(anim_clips.DecodeError, "channel definitions end"):
            anim_clips.decode_animation(s, anim)

    def test_pointer_outside_the_pages(self):
        b = bytearray(two_sequence_dictionary())
        struct.pack_into("<Q", b, 0x48, SYSTEM + len(b) + 0x100)      # clip map buckets
        with self.assertRaisesRegex(anim_clips.DecodeError, "outside the pages"):
            anim_clips.parse_dictionary(anim_clips.System(bytes(b)))

    def test_inline_name(self):
        b = bytearray(two_sequence_dictionary("short@name"))
        b[0x38:0x48] = b"short@name".ljust(15, b"\x00") + bytes([0x80 | 10])
        d = anim_clips.parse_dictionary(anim_clips.System(bytes(b)))
        self.assertEqual(d["name"], "short@name")
        self.assertEqual(d["hash"], joaat32("short@name"))

    def test_motion_with_an_inline_name(self):
        b = bytearray(two_sequence_dictionary())
        motion = {k & 0xFFFFFFFF: v for k, v in self.s.atmap(0x48)}[2]
        b[motion + 0x30:motion + 0x40] = b"walk.motion".ljust(15, b"\x00") + bytes([0x80 | 11])
        d = anim_clips.parse_dictionary(anim_clips.System(bytes(b)))
        self.assertEqual(sorted((c["kind"], c["name"]) for c in d["clips"]),
                         [("clip", "pack:/one.clip"), ("motion", "walk.motion"),
                          ("motionmatcher", "pack:/one.motionmatcher")])

    def test_rig_root_is_not_a_clip_dictionary(self):
        b = bytearray(two_sequence_dictionary())
        b[0x70:0x78] = b"RIG\x02\x10\x00\x00\x00"
        with self.assertRaisesRegex(anim_clips.NotClipDictionary, "rig"):
            anim_clips.parse_dictionary(anim_clips.System(bytes(b)))


def write_raw(path, parts):
    """parts: [(bytes, compress?)] written 16-byte aligned after a minimal RPF9 header."""
    body = bytearray(0x100)
    body[:4] = b"9FPR"
    struct.pack_into("<I", body, 4, 0x100)
    offsets = []
    for data, compress in parts:
        offsets.append(len(body))
        body += zlib.compress(data) if compress else data
        body += bytes((-len(body)) % 16)
    body += bytes(64)
    with open(path, "wb") as f:
        f.write(body)
    return offsets


class GapPageTest(unittest.TestCase):
    """A dictionary's last system page that anim_index does not list: a zlib stream inflating to fewer than 64
    bytes, or a 16-byte raw page. Both are joined only when the pages then parse."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rpf = os.path.join(self.tmp.name, "gap.rpf")

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, parts):
        write_raw(self.rpf, parts)
        return anim_clips.scan(self.rpf)

    def test_small_zlib_last_page(self):
        page = two_sequence_dictionary("small@dict")
        a, b = split(page, len(page) - 32, move_map=False)
        rows, summary = self.scan([(a, True), (b, True), (two_sequence_dictionary("next@dict"), True)])
        self.assertEqual(summary["streams"], 2)                 # anim_index skips the 32-byte page
        self.assertEqual(summary["gap_pages"], {"small zlib page": 1})
        self.assertEqual(summary["unresolved"], {})
        self.assertEqual(summary["counts"]["dictionaries"], 2)
        self.assertEqual(summary["counts"]["animations_decoded"], 2)
        self.assertEqual(summary["pages"], {"1": 1, "2": 1})

    def test_raw_16_byte_last_page(self):
        page = two_sequence_dictionary("raw@dict")
        a, b = split(page, len(page) - 16, move_map=False)
        rows, summary = self.scan([(a, True), (b, False), (two_sequence_dictionary("next@dict"), True)])
        self.assertEqual(summary["gap_pages"], {"raw 16-byte page": 1})
        self.assertEqual(summary["counts"]["dictionaries"], 2)
        self.assertEqual(summary["counts"]["animations_decoded"], 2)

    def test_page_map_in_an_unlisted_last_page_stays_unresolved(self):
        # The tail is tried only after the page map has been read from listed streams (see docs/animation.md).
        for raw, size in ((False, 16), (True, 32)):
            page = two_sequence_dictionary("map@dict")
            a, b = split(page, len(page) + 16 - size)          # move_map: the page map is the last 16 bytes
            rows, summary = self.scan([(a, True), (b, raw), (two_sequence_dictionary("next@dict"), True)])
            self.assertEqual(summary["unresolved"], {"page map not in the next stream": 1})
            self.assertEqual(summary["counts"]["dictionaries"], 1)

    def test_gap_that_does_not_parse_stays_unresolved(self):
        page = two_sequence_dictionary("cut@dict")
        a, _b = split(page, 0x400, move_map=False)              # the rest of the page is missing
        rows, summary = self.scan([(a, True), (b"\x55" * 64, False), (two_sequence_dictionary("next@dict"), True)])
        self.assertEqual(summary["gap_pages"], {})
        self.assertEqual(summary["unresolved"], {"system page not the next stream": 1})
        self.assertEqual(summary["counts"]["dictionaries"], 1)


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rpf = os.path.join(self.tmp.name, "synthetic.rpf")
        second, nowhere = joaat32("second@dict"), joaat32("nowhere@dict")
        clip_duration = (NF - 1) / 30.0                    # the `.clip` (hash 1) in every synthetic dictionary
        one = two_sequence_dictionary("first@dict", children=[(1, second), (7, second), (1, nowhere)],
                                      external_hashes=[second, nowhere],
                                      times=[(clip_duration, clip_duration, [(0.0, 0.0), (clip_duration,) * 2]),
                                             (2.0, 2.0, [(0.0, 0.0), (2.0, 2.0)]),
                                             (2.0, 3.0, [(0.0, 0.0), (1.0, 2.0), (2.0, 3.0)])],
                                      axis=([joaat32("yawangle")], [(2, -1.0), (0, 0.0), (1, 2.0)], -1.0, 2.0))
        a, b = split(two_sequence_dictionary("second@dict"), 0x400)
        c, d = split(two_sequence_dictionary("third@dict"), 0x400, move_map=False)
        other = bytearray(0x60)
        struct.pack_into("<Q", other, 8, 0x50000020)
        other[16:20] = b"0DRP"
        struct.pack_into("<I", other, 0x14, 122)
        # stream 5 (third dictionary's second page) sits after a gap, so it is not the next stream
        self.offsets = write_rpf(self.rpf, [one, a, b, bytes(other), c, d], gap_before={5})

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_counts(self):
        rows, summary = anim_clips.scan(self.rpf)
        self.assertEqual(summary["streams"], 6)
        self.assertEqual(summary["dictionary_roots"], 3)
        self.assertEqual(summary["dictionaries_grouped"], 2)
        self.assertEqual(summary["unresolved"], {"system page not the next stream": 1})
        self.assertEqual(summary["pages"], {"1": 1, "2": 1})
        c = summary["counts"]
        self.assertEqual((c["dictionaries"], c["clips"], c["clips_motion"], c["clips_motionmatcher"],
                          c["animations"], c["animations_decoded"], c["sequences"]), (2, 6, 2, 2, 2, 2, 4))
        self.assertEqual(c["dictionary_hash_matches_name"], 2)
        self.assertEqual(c["frames_sum_matches"], 2)
        self.assertEqual(c["boundary_quaternions"], c["boundary_quaternions_same_rotation"])
        self.assertEqual(c["track_type:bonerotation"], 4)
        # `.motion` children are {clip hash, dictionary hash}; root refs without a pointer name dictionaries
        self.assertEqual((c["motion_children"], c["motion_children_in_external_refs"],
                          c["motion_children_dictionary_here"], c["motion_children_resolved_to_clip"]), (3, 3, 2, 1))
        self.assertEqual((c["external_refs_to_a_dictionary_here"], c["external_hashes_distinct"],
                          c["external_hashes_distinct_found_here"]), (1, 2, 1))
        self.assertEqual(c["motion_parameters:1"], 2)
        # +0x70 child times: one per child; the resolved child's duration equals its `.clip`'s
        self.assertEqual((c["motion_times_one_per_child"], c["motion_child_times"], c["motion_child_times_identity"],
                          c["motion_child_times_warped"], c["motion_child_duration_equals_clip"]), (2, 3, 2, 1, 1))
        self.assertEqual((c["motion_node_type:3"], c["motion_axes"], c["motion_axis_samples"],
                          c["motion_axis_indices_are_0_to_n"], c["motion_axis_name:yawangle"]), (1, 1, 3, 1, 1))
        self.assertEqual(c["motion_node_unreadable"], 1)                    # the second dictionary has no node
        self.assertNotIn("_children", rows[0])
        self.assertNotIn("_child_durations", rows[0])
        self.assertEqual(summary["gap_pages"], {})
        self.assertEqual([r["offset"] for r in rows], [self.offsets[0], self.offsets[1]])
        self.assertEqual([r["streams"] for r in rows], [1, 2])

    def test_jobs_give_the_same_counts(self):
        rows1, one = anim_clips.scan(self.rpf, jobs=1)
        rows2, two = anim_clips.scan(self.rpf, jobs=2)
        for summary in (one, two):
            summary.pop("slowest_dictionary_seconds")
        self.assertEqual(one, two)
        self.assertEqual([r["offset"] for r in rows1], [r["offset"] for r in rows2])

    def test_budget_skips_instead_of_blocking(self):
        _rows, summary = anim_clips.scan(self.rpf, budget=0)
        c = summary["counts"]
        self.assertEqual(c["animations_skipped_over_budget"], 2)
        self.assertEqual(c["dictionaries_over_budget"], 2)

    def test_cli_writes_counts_and_no_values(self):
        out = os.path.join(self.tmp.name, "out.json")
        r = subprocess.run([sys.executable, "-I", TOOL, self.rpf, out, "--quiet"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["summary"]["counts"]["animations_decoded"], 2)
        for row in doc["dictionaries"]:      # per-dictionary rows hold counts, never values
            self.assertTrue(all(not isinstance(v, (list, dict)) for v in row.values()), row)

    def test_decode_rejects_negative_frames(self):
        result = subprocess.run(
            [sys.executable, "-I", TOOL, self.rpf, "--decode", str(self.offsets[1]), "--frames", "-1"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--frames must be 0 or more", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_decode_prints_values(self):
        d = anim_clips.decode_one(self.rpf, self.offsets[1], 2)
        anim = d["packs"][0]["animations"][0]
        self.assertEqual(anim["decoded_frames"], NF)
        self.assertEqual(anim["decoded"]["2"], [[1.0, 2.0, 3.0]] * 2)
        self.assertEqual(anim["tracks"][0]["type"], "bonerotation")
        with self.assertRaisesRegex(ValueError, "no clip dictionary"):
            anim_clips.decode_one(self.rpf, self.offsets[3], 2)

    def test_decode_prints_track_values_from_a_six_page_dictionary(self) -> None:
        data = bytearray(two_sequence_dictionary("six-pages@dict"))
        page_map = struct.unpack_from("<Q", data, 8)[0] - SYSTEM
        data[page_map + 8] = 6
        pages = [data[i:i + 256] for i in range(0, 5 * 256, 256)] + [data[5 * 256:]]
        self.assertTrue(all(len(page) >= 64 for page in pages))
        path = os.path.join(self.tmp.name, "six-pages.rpf")
        root, *_ = write_rpf(path, pages)
        _, summary = anim_clips.scan(path)
        self.assertEqual(summary["pages"], {"6": 1})
        self.assertEqual(summary["counts"]["animations_decoded"], 1)

        result = subprocess.run(
            [sys.executable, "-I", TOOL, path, "--decode", str(root), "--frames", "3"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        animation = json.loads(result.stdout)["packs"][0]["animations"][0]
        self.assertEqual(animation["decoded_frames"], NF)
        self.assertEqual(animation["decoded"]["2"], [[1.0, 2.0, 3.0]] * 3)
        self.assertEqual(animation["decoded"]["4"], [[0.0], [1.5], [3.0]])

    def test_decode_page_counts_on_or_across_sixth_page(self):
        data = bytearray(two_sequence_dictionary())
        pm = len(data)
        data.extend(bytes(96))
        struct.pack_into("<Q", data, 8, SYSTEM + pm)
        data[pm + 8] = 6
        for last_page in (pm, pm + 9):
            cuts = [0, 256, 512, 768, 1024, last_page, len(data)]
            pages = [data[a:b] for a, b in zip(cuts, cuts[1:])]
            root, *_ = write_rpf(self.rpf, pages)
            result = subprocess.run([sys.executable, "-I", TOOL, self.rpf, "--decode", str(root)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["packs"][0]["animations"][0]["decoded_frames"], NF)

    def test_invalid_map_cannot_borrow_counts_from_next_root(self):
        data = bytearray(two_sequence_dictionary())
        for pm in (len(data) + 64, 0xfffffff):
            struct.pack_into("<Q", data, 8, SYSTEM + pm)
            following = bytearray(two_sequence_dictionary())
            following[72] = 2
            write_rpf(self.rpf, [data, following])
            _, summary = anim_clips.scan(self.rpf)
            self.assertEqual(summary["unresolved"], {"page map not in the next stream": 1})
            self.assertEqual(summary["pages"], {"1": 1})

    def test_budget_rejects_nan_and_negative(self):
        for value in ("nan", "-1"):
            r = subprocess.run(
                [sys.executable, "-I", TOOL, self.rpf, os.path.join(self.tmp.name, "o.json"),
                 "--budget", value],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 2, value)
            self.assertIn("--budget", r.stderr)
            self.assertEqual(r.stdout, "")

    def test_bad_arguments(self):
        for args in (["--jobs", "0", "x.json"], [], ["x.json", "--start", "-1"]):
            r = subprocess.run([sys.executable, "-I", TOOL, self.rpf, *args], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)
        missing = os.path.join(self.tmp.name, "missing.rpf")
        r = subprocess.run([sys.executable, "-I", TOOL, missing, os.path.join(self.tmp.name, "o.json")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing.rpf", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)
        empty = os.path.join(self.tmp.name, "empty.rpf")
        open(empty, "wb").close()
        for source in (empty, self.tmp.name):
            r = subprocess.run([sys.executable, "-I", TOOL, source, os.path.join(self.tmp.name, "o.json")],
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, source)
            self.assertNotIn("Traceback", r.stderr)


    def test_output_cannot_overwrite_source_archive(self):
        with open(self.rpf, "rb") as f:
            data = f.read()
        other = os.path.join(os.path.dirname(self.rpf), ".", os.path.basename(self.rpf))
        r = subprocess.run([sys.executable, "-I", TOOL, self.rpf, other, "--quiet"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("output must not overwrite the input archive", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        with open(self.rpf, "rb") as f:
            self.assertEqual(f.read(), data)

    def test_output_that_is_a_directory_is_refused_before_the_scan(self):
        r = subprocess.run([sys.executable, "-I", TOOL, self.rpf, self.tmp.name, "--quiet"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("is a directory", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_output_in_a_missing_folder_is_created(self):
        out = os.path.join(self.tmp.name, "reports", "sub", "out.json")
        r = subprocess.run([sys.executable, "-I", TOOL, self.rpf, out, "--quiet"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["summary"]["counts"]["animations_decoded"], 2)


if __name__ == "__main__":
    unittest.main()
