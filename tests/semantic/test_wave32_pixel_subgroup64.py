"""Wave32 pixel shaders stay isolated when Vulkan runs them in a 64-lane subgroup.

The inputs are deliberately different in the two physical halves. Each translated PS is run both as one 64-lane
subgroup and as two 32-lane subgroups; outputs must match the independent logical-wave32 expectations.
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)

import support
import spirv_mini_interp as si


def fbits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def run_ps(spv, subgroup_size, inputs):
    _, outputs = si.run_workgroup(
        spv, 64, subgroup_size,
        inputs_for=lambda i: {
            "%sg_lane": i % subgroup_size,
            "%in_attr0": (inputs[i], 0.0, 0.0, 0.0),
        },
        return_outputs=True,
    )
    return [out["%out_mrt0"] for out in outputs]


class Wave32PixelSubgroupTests(unittest.TestCase):
    def test_lane_reads_and_writes_stay_in_each_half(self):
        spv = support.translate_lines("ps_wave32_halves_lane_ops", [
            "v_interp_mov_f32_e32 v2, p0, attr0.x",
            "v_readfirstlane_b32 s20, v2",
            "v_readlane_b32 s21, v2, 0",
            "v_mov_b32_e32 v6, s20",
            "v_mov_b32_e32 v7, s21",
            "v_mov_b32_e32 v8, 0",
            "v_writelane_b32 v8, s20, 0",
            "v_mov_b32_e32 v3, 0",
            "ds_bpermute_b32 v9, v3, v2",
            "exp mrt0 v6, v7, v8, v9 done vm",
            "s_endpgm",
        ])
        inputs = [float(i + 1) for i in range(64)]
        expected = []
        for i in range(64):
            first = fbits(float((i // 32) * 32 + 1))
            expected.append((first, first, first if i % 32 == 0 else 0, first))
        for subgroup_size in (32, 64):
            with self.subTest(subgroup_size=subgroup_size):
                self.assertEqual(run_ps(spv, subgroup_size, inputs), expected)

    def test_ds_permute_scatter_stays_in_each_half(self):
        spv = support.translate_lines("ps_wave32_halves_ds_permute", [
            "v_interp_mov_f32_e32 v2, p0, attr0.x",
            "v_mbcnt_lo_u32_b32 v5, -1, 0",
            "v_lshlrev_b32_e32 v3, 2, v5",
            "ds_permute_b32 v4, v3, v2",
            "exp mrt0 v4, v2, v2, v2 done vm",
            "s_endpgm",
        ])
        inputs = [float(i + 1) for i in range(64)]
        expected = [(fbits(value),) * 4 for value in inputs]
        for subgroup_size in (32, 64):
            with self.subTest(subgroup_size=subgroup_size):
                self.assertEqual(run_ps(spv, subgroup_size, inputs), expected)

    def test_ballots_and_votes_stay_in_each_half(self):
        spv = support.translate_lines("ps_wave32_halves_ballot", [
            "v_interp_mov_f32_e32 v1, p0, attr0.x",
            "v_cmp_ne_u32_e32 vcc_lo, 0, v1",
            "s_bcnt1_i32_b32 s20, vcc_lo",
            "s_cmp_lg_u32 vcc_lo, 0",
            "s_cselect_b32 s21, 1, 0",
            "s_cmp_eq_u32 vcc_lo, exec_lo",
            "s_cselect_b32 s22, 1, 0",
            "v_mov_b32_e32 v6, s20",
            "v_mov_b32_e32 v7, s21",
            "v_mov_b32_e32 v8, s22",
            "exp mrt0 v6, v7, v8, v8 done vm",
            "s_endpgm",
        ])
        inputs = [0.0] * 32 + [1.0] * 32
        expected = [(0, 0, 0, 0)] * 32 + [(32, 1, 1, 1)] * 32
        for subgroup_size in (32, 64):
            with self.subTest(subgroup_size=subgroup_size):
                self.assertEqual(run_ps(spv, subgroup_size, inputs), expected)


if __name__ == "__main__":
    unittest.main()
