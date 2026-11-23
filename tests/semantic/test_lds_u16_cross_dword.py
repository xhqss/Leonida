"""16-bit LDS reads and writes at byte 3 of a dword, which cross into the next dword.

RDNA2 LDS in unaligned mode reads and writes bytes 3 and 4 for a halfword at byte 3: llc -mcpu=gfx1030
-mattr=+unaligned-access-mode emits a single `ds_read_u16 ... offset:3` / `ds_write_b16 ... offset:3` for an align-1
i16 LDS access (without that mode it splits the access into two byte accesses). Before this fix the reads extracted
16 bits at bit 24 of one dword (undefined in SPIR-V) and the writes kept only byte 3. Hand-written wave32 compute
listings, translated and run in spirv_mini_interp; no GPU or game files.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import spirv_mini_interp as si  # noqa: E402
import support  # noqa: E402
from test_cs_shared_memory_limit import out, run_one  # noqa: E402
from test_lds_subword_d16 import FSSM, VSHARP  # noqa: E402

WORDS = [0x44332211, 0x88776655, 0xCCBBAA99]
LDS = b"".join(w.to_bytes(4, "little") for w in WORDS)
PRE = 0xDEADBEEF
DATA = 0xA1B2C3D4


def init_lds():
    lines = []
    for k, w in enumerate(WORDS):
        lines += [f"v_mov_b32_e32 v1, {4 * k}", f"v_mov_b32_e32 v2, {w:#x}", "ds_write_b32 v1, v2"]
    return lines + ["s_waitcnt lgkmcnt(0)"]


def run(lines, words):
    spv = support.translate_lines("cs_lds_u16_cross", lines + out(*range(10, 10 + words)) + ["s_endpgm"],
                                  fssm=FSSM)
    memory = {}
    si.run_fragment(spv, inputs={"%wg_id": (0, 0, 0), "%local_id": (0, 0, 0), "%sg_lane": 0},
                    push_constants=VSHARP + [0] * 28, memory=memory)
    return [memory.get(4 * k) for k in range(words)]


def sext16(v):
    return (v - 0x10000 if v & 0x8000 else v) & 0xFFFFFFFF


def expected_read(op, addr):
    h = int.from_bytes(LDS[addr:addr + 2], "little")
    return {"ds_read_u16": h, "ds_read_i16": sext16(h), "ds_read_u16_d16": (PRE & 0xFFFF0000) | h,
            "ds_read_u16_d16_hi": (PRE & 0xFFFF) | (h << 16)}[op]


def expected_words(addr, half):
    b = bytearray(LDS)
    b[addr:addr + 2] = half.to_bytes(2, "little")
    return [int.from_bytes(b[4 * k:4 * k + 4], "little") for k in range(len(WORDS))]


class LdsU16CrossDwordTests(unittest.TestCase):
    def test_reads(self):
        # byte 3 (vaddr), byte 7 (offset:7), and the non-crossing halfwords around them, unchanged
        for op in ("ds_read_u16", "ds_read_i16", "ds_read_u16_d16", "ds_read_u16_d16_hi"):
            for vaddr, off in ((3, 0), (0, 7), (4, 3), (1, 0), (2, 0), (5, 0), (0, 6)):
                with self.subTest(op=op, vaddr=vaddr, offset=off):
                    mod = f" offset:{off}" if off else ""
                    got = run(init_lds() + [f"v_mov_b32_e32 v4, {vaddr}", f"v_mov_b32_e32 v10, {PRE:#x}",
                                            f"{op} v10, v4{mod}", "s_waitcnt lgkmcnt(0)"], 1)
                    self.assertEqual(got, [expected_read(op, vaddr + off)])

    def test_writes(self):
        for op, half in (("ds_write_b16", DATA & 0xFFFF), ("ds_write_b16_d16_hi", DATA >> 16)):
            for vaddr, off in ((3, 0), (0, 7), (4, 3), (1, 0), (2, 0), (6, 0)):
                with self.subTest(op=op, vaddr=vaddr, offset=off):
                    mod = f" offset:{off}" if off else ""
                    got = run(init_lds() + [f"v_mov_b32_e32 v4, {vaddr}", f"v_mov_b32_e32 v5, {DATA:#x}",
                                            f"{op} v4, v5{mod}", "s_waitcnt lgkmcnt(0)", "v_mov_b32_e32 v1, 0",
                                            "ds_read_b32 v10, v1", "ds_read_b32 v11, v1 offset:4",
                                            "ds_read_b32 v12, v1 offset:8", "s_waitcnt lgkmcnt(0)"], 3)
                    self.assertEqual(got, expected_words(vaddr + off, half))

    def test_chained_crossing_writes_keep_each_others_bytes(self):
        # Halfwords at bytes 3, 7 and 11: each one's high byte and the next one's low byte share a dword, so each
        # write must keep the other bytes of both dwords it touches. Read back as halfwords and as dwords.
        lines = []
        for k in range(4):
            lines += [f"v_mov_b32_e32 v1, {4 * k}", "v_mov_b32_e32 v2, 0", "ds_write_b32 v1, v2"]
        lines += ["v_mov_b32_e32 v1, 0"]
        for k, h in enumerate((0x1141, 0x2242, 0x3343)):
            lines += [f"v_mov_b32_e32 v2, {h:#x}", f"ds_write_b16 v1, v2 offset:{4 * k + 3}"]
        lines += ["s_waitcnt lgkmcnt(0)", "ds_read_u16 v10, v1 offset:3", "ds_read_u16 v11, v1 offset:7",
                  "ds_read_u16 v12, v1 offset:11", "ds_read_b32 v13, v1", "ds_read_b32 v14, v1 offset:4",
                  "ds_read_b32 v15, v1 offset:8", "ds_read_b32 v16, v1 offset:12", "s_waitcnt lgkmcnt(0)"]
        self.assertEqual(run(lines, 7), [0x1141, 0x2242, 0x3343, 0x41000000, 0x42000011, 0x43000022, 0x33])

    def test_crossing_past_the_allocation_reads_zero_and_drops_the_high_byte(self):
        # 512 B allocated (offset:1024 elsewhere makes the shader bounds-checked). A halfword at byte 511 has its
        # high byte at byte 512: written nowhere (word 0 keeps its sentinel), read as 0.
        _, mem = run_one(["v_mov_b32_e32 v1, 0", "v_mov_b32_e32 v2, 0x5e17", "ds_write_b32 v1, v2",
                          "v_mov_b32_e32 v3, 0x1fc", "v_mov_b32_e32 v2, 0x00ffffff", "ds_write_b32 v3, v2",
                          "s_waitcnt lgkmcnt(0)", "v_mov_b32_e32 v4, 0xc3d4", "ds_write_b16 v3, v4 offset:3",
                          "s_waitcnt lgkmcnt(0)", "v_mov_b32_e32 v5, 0xdead", "v_mov_b32_e32 v6, 0xdead",
                          "v_mov_b32_e32 v7, 0xdead", "ds_read_u16 v5, v3 offset:3", "ds_read_b32 v6, v1",
                          "ds_read_b32 v7, v3", "v_mov_b32_e32 v8, 0xdead", "ds_read_b32 v8, v1 offset:1024",
                          "s_waitcnt lgkmcnt(0)"] + out(5, 6, 7, 8), 1)
        self.assertEqual([mem.get(4 * k) for k in range(4)], [0xD4, 0x5E17, 0xD4FFFFFF, 0])


if __name__ == "__main__":
    unittest.main()
