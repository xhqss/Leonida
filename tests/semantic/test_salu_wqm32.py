"""Translator-to-interpreter semantic tests for s_wqm_b32:

  s_wqm_b32 D, S0   D = every 4-bit quad of S0 that has any bit set becomes 0xF (else 0x0); SCC = (D != 0).

Covers both:
  - Data path (32-bit nibble expansion + SCC) via tools/rdna2spirv.py -> spirv_mini_interp.py.
  - Lane-mask path (32-lane wave32, 8 quads) via tools/rdna2spirv.py -> spirv_wave_interp.py (vertex stage
    uses OpGroupNonUniformShuffle for quad_any; fragment/compute stages are checked for OpGroupNonUniformQuadSwap).

Standard library only; no game data.
"""
import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import spirv_wave_interp as wi  # noqa: E402
from support import mrt0_words, translate_lines  # noqa: E402

MASK32 = 0xFFFFFFFF


def ref_wqm32(x):
    x &= MASK32
    out = 0
    for nibble in range(8):
        shift = nibble * 4
        if (x >> shift) & 0xF:
            out |= 0xF << shift
    return out, int(out != 0)


HAND_CONSTANTS = [
    (0x00000000, 0x00000000, 0),
    (0xFFFFFFFF, 0xFFFFFFFF, 1),
    (0x00000001, 0x0000000F, 1),
    (0x80000000, 0xF0000000, 1),
    (0x80000001, 0xF000000F, 1),
    (0x12480001, 0xFFFF000F, 1),
    (0x01020408, 0x0F0F0F0F, 1),
    (0x00100020, 0x00F000F0, 1),
    (0x40000000, 0xF0000000, 1),
]


def run_ps(name, body_lines, export_regs=("v0", "v1", "v0", "v1")):
    lines = list(body_lines) + [f"exp mrt0 {', '.join(export_regs)} done vm", "s_endpgm"]
    spv = translate_lines(name, lines)
    return mrt0_words(spv), spv


class ReferenceConstantsTests(unittest.TestCase):
    def test_hand_computed_constants(self):
        for val, exp_d, exp_scc in HAND_CONSTANTS:
            with self.subTest(val=hex(val)):
                self.assertEqual(ref_wqm32(val), (exp_d, exp_scc))


class DataPathWqm32Tests(unittest.TestCase):
    def test_single_bit_across_all_32_positions(self):
        for bit in range(32):
            src = 1 << bit
            exp_d, exp_scc = ref_wqm32(src)
            with self.subTest(bit=bit):
                words, _ = run_ps(
                    "ps_wqm_bit",
                    [
                        f"s_mov_b32 s0, {src:#x}",
                        "s_wqm_b32 s1, s0",
                        "s_cselect_b32 s2, 1, 0",
                        "v_mov_b32 v0, s1",
                        "v_mov_b32 v1, s2",
                    ],
                )
                self.assertEqual((words[0], words[1]), (exp_d, exp_scc))

    def test_multi_nibble_patterns_and_scc_toggling(self):
        for val, exp_d, exp_scc in HAND_CONSTANTS:
            with self.subTest(val=hex(val)):
                words, _ = run_ps(
                    "ps_wqm_const",
                    [
                        f"s_mov_b32 s0, {val:#x}",
                        "s_wqm_b32 s1, s0",
                        "s_cselect_b32 s2, 1, 0",
                        "v_mov_b32 v0, s1",
                        "v_mov_b32 v1, s2",
                    ],
                )
                self.assertEqual((words[0], words[1]), (exp_d, exp_scc))

        toggle_words, _ = run_ps(
            "ps_wqm_toggle",
            [
                "s_mov_b32 s0, 0",
                "s_wqm_b32 s1, s0",
                "s_cselect_b32 s4, 1, 0",
                "s_mov_b32 s0, 0x00001000",
                "s_wqm_b32 s2, s0",
                "s_cselect_b32 s5, 1, 0",
                "s_mov_b32 s0, 0",
                "s_wqm_b32 s3, s0",
                "s_cselect_b32 s6, 1, 0",
                "v_mov_b32 v0, s4",
                "v_mov_b32 v1, s5",
                "v_mov_b32 v2, s6",
                "v_mov_b32 v3, s2",
            ],
            export_regs=("v0", "v1", "v2", "v3"),
        )
        self.assertEqual(toggle_words, [0, 1, 0, 0x0000F000])

    def test_inline_and_literal_operands(self):
        for operand, src_val in (("0", 0), ("1", 1), ("15", 15), ("-1", MASK32), ("0x10200004", 0x10200004)):
            exp_d, exp_scc = ref_wqm32(src_val)
            with self.subTest(operand=operand):
                words, _ = run_ps(
                    "ps_wqm_imm",
                    [
                        f"s_wqm_b32 s1, {operand}",
                        "s_cselect_b32 s2, 1, 0",
                        "v_mov_b32 v0, s1",
                        "v_mov_b32 v1, s2",
                    ],
                )
                self.assertEqual((words[0], words[1]), (exp_d, exp_scc))

    def test_seeded_random_values(self):
        rng = random.Random(0x57514D32)
        for _ in range(16):
            val = rng.getrandbits(32)
            exp_d, exp_scc = ref_wqm32(val)
            with self.subTest(val=hex(val)):
                words, _ = run_ps(
                    "ps_wqm_rand",
                    [
                        f"s_mov_b32 s0, {val:#x}",
                        "s_wqm_b32 s1, s0",
                        "s_cselect_b32 s2, 1, 0",
                        "v_mov_b32 v0, s1",
                        "v_mov_b32 v1, s2",
                    ],
                )
                self.assertEqual((words[0], words[1]), (exp_d, exp_scc))


class LaneMaskWqm32Tests(unittest.TestCase):
    def test_lane_mask_wave32_quad_expansion_in_wave_interp(self):
        test_masks = (0x00000000, 0x00000001, 0x00000080, 0x12480001, 0x80000000, 0xFFFFFFFF)
        for mask in test_masks:
            exp_mask, exp_scc = ref_wqm32(mask)
            with self.subTest(mask=hex(mask)):
                spv = translate_lines(
                    "vs_wqm_mask",
                    [
                        "v_and_b32_e32 v1, 31, v0",
                        "v_lshlrev_b32_e32 v2, v1, 1",
                        f"v_and_b32_e32 v2, {mask:#x}, v2",
                        "v_cmp_ne_u32_e32 vcc_lo, 0, v2",
                        "s_wqm_b32 s0, vcc_lo",
                        "s_cselect_b32 s1, 1, 0",
                        "v_cndmask_b32_e64 v3, 0, 1, s0",
                        "v_mov_b32 v4, s1",
                        "exp pos0 v3, v4, v1, v1 done",
                        "s_endpgm",
                    ],
                )
                outputs = wi.run_wave(
                    spv,
                    wave=32,
                    builtins={"VertexIndex": lambda lane: lane, "InstanceIndex": lambda lane: 0},
                )["outputs"]
                self.assertEqual(len(outputs), 32)
                got_mask = 0
                for lane, out in enumerate(outputs):
                    self.assertIsNotNone(out)
                    self.assertEqual(len(out), 1)
                    active_word, scc_word, lane_word, _ = out[next(iter(out))]
                    self.assertEqual(lane_word, lane)
                    self.assertEqual(scc_word, exp_scc)
                    if active_word:
                        got_mask |= 1 << lane
                self.assertEqual(got_mask, exp_mask)

    def test_ps_lane_mask_emits_quad_swap_and_vote_any(self):
        spv = translate_lines(
            "ps_wqm_mask_struct",
            [
                "v_mbcnt_lo_u32_b32 v0, -1, 0",
                "v_cmp_eq_u32_e32 vcc_lo, 0, v0",
                "s_wqm_b32 s0, vcc_lo",
                "s_cselect_b32 s1, 1, 0",
                "v_cndmask_b32_e64 v1, 0, 1, s0",
                "v_mov_b32 v2, s1",
                "exp mrt0 v1, v2, v1, v2 done vm",
                "s_endpgm",
            ],
        )
        self.assertGreaterEqual(spv.count("OpGroupNonUniformQuadSwap %uint"), 2)
        self.assertIn("OpGroupNonUniformAny %bool", spv)


if __name__ == "__main__":
    unittest.main()
